import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .engines import default_registry
from .maintenance import database_copies
from .models import HostApproval, RunInput
from .queue import enqueue, public_job
from .ssh import public_metadata, scan_host
from .store import now
from .operations_api import install_operations


def runtime_status(store, settings):
    with store.connect() as db:
        state = dict(db.execute('SELECT name,value FROM worker_state'))
    last = state.get('heartbeat')
    alive = bool(last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 45)
    copies = database_copies(settings.database_backup_dir)
    return {'worker': {'online': alive, 'heartbeat': last},'storage':json.loads(state.get('storage','null')),
            'database_backup': {'interval_seconds': settings.database_backup_interval,
                'keep': settings.database_backup_keep, 'latest': copies[0] if copies else None,
                'error': state.get('database_error') or None}}


def install_runtime(api, settings):
    registry = default_registry()
    install_operations(api,settings)

    def store():
        return api.state.store

    def launch(backup_id, kind, data):
        try:
            return public_job(enqueue(store(), backup_id, kind, idempotency_key=data.idempotency_key, registry=registry))
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.get('/api/v1/engines')
    def engines():
        return registry.describe()

    @api.get('/api/v1/servers/{identifier}/host-key')
    def scan(identifier: str):
        with store().connect() as db:
            row = db.execute('SELECT * FROM servers WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Serveur introuvable.')
        try:
            return scan_host(dict(row))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @api.post('/api/v1/servers/{identifier}/host-key')
    def approve(identifier: str, data: HostApproval):
        try:
            fingerprint = public_metadata(data.public_key)
        except (ValueError, TypeError):
            raise HTTPException(422, 'Clé d’hôte invalide.') from None
        if fingerprint != data.fingerprint:
            raise HTTPException(422, 'L’empreinte ne correspond pas à la clé publique.')
        with store().connect() as db:
            row = db.execute('SELECT * FROM servers WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Serveur introuvable.')
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') AND json_extract(configuration,'$.server_id')=?", (identifier,)).fetchone():
                raise HTTPException(409, 'Attendre la fin des opérations avant de changer la clé d’hôte.')
            db.execute('INSERT OR REPLACE INTO trusted_hosts VALUES (?,?,?,?,?,?)',
                       (identifier, row['host'], row['port'], data.public_key, fingerprint, now()))
            store().audit(db, 'server.host_approved', identifier)
        return {'fingerprint': fingerprint}

    @api.post('/api/v1/backups/{identifier}/run', status_code=202)
    def run(identifier: str, data: RunInput):
        return launch(identifier, 'backup', data)

    @api.post('/api/v1/backups/{identifier}/test', status_code=202)
    def test(identifier: str, data: RunInput):
        return launch(identifier, 'test', data)

    @api.get('/api/v1/jobs')
    def jobs(backup_id: str | None = None):
        with store().connect() as db:
            rows = db.execute('SELECT * FROM jobs WHERE (? IS NULL OR backup_id=?) ORDER BY created_at DESC LIMIT 100', (backup_id, backup_id))
            return [public_job(row, include_log=False) for row in rows]

    @api.get('/api/v1/jobs/{identifier}')
    def job(identifier: str):
        with store().connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Opération introuvable.')
            return public_job(row)

    @api.post('/api/v1/jobs/{identifier}/cancel')
    def cancel(identifier: str):
        with store().connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Opération introuvable.')
            if row['state'] not in ('queued', 'running') or row['kind']=='database':
                raise HTTPException(409, 'Cette opération ne peut pas être annulée.')
            db.execute("UPDATE jobs SET state='cancelled',finished_at=? WHERE id=?", (now(), identifier))
            store().audit(db, 'job.cancelled', identifier)
        return {'state': 'cancelled'}

    @api.get('/api/v1/database-backups')
    def copies():
        return database_copies(settings.database_backup_dir)

    @api.post('/api/v1/database-backups', status_code=202)
    def database_backup():
        try:
            return public_job(enqueue(store(), kind='database'))
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.get('/api/v1/database-backups/{identifier}/download')
    def download_database(identifier: str):
        import fcntl
        try:
            lease = open(settings.database_backup_dir / '.database.lock', 'rb')
        except FileNotFoundError:
            raise HTTPException(404, 'Aucune copie disponible.') from None
        fcntl.flock(lease, fcntl.LOCK_SH)
        try:
            row = next((r for r in database_copies(settings.database_backup_dir) if r['id']==identifier), None)
            if not row:
                raise HTTPException(404, 'Copie SQLite introuvable.')
            return FileResponse(settings.database_backup_dir / row['filename'], filename=row['filename'],
                                media_type='application/octet-stream', background=BackgroundTask(lease.close))
        except BaseException:
            lease.close()
            raise

    @api.get('/api/v1/snapshots')
    def snapshots(backup_id: str | None = None):
        with store().connect() as db:
            result = []
            for row in db.execute("SELECT * FROM snapshots WHERE state='ready' AND (? IS NULL OR backup_id=?) ORDER BY created_at DESC", (backup_id, backup_id)):
                config = json.loads(row['configuration'])
                result.append({'id': row['id'], 'backup_id': row['backup_id'], 'name': config['name'],
                    'engine': row['engine'], 'revision': config['revision'], 'created_at': row['created_at'],
                    'folders': config['folders'],'checksum_available':(settings.backup_dir/'integrity'/f"{row['id']}.json").exists()})
            return result

    def snapshot_path(identifier, path):
        import fcntl
        with store().connect() as db:
            row = db.execute("SELECT 1 FROM snapshots WHERE id=? AND state='ready'", (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Version introuvable.')
        root = settings.backup_dir / 'snapshots' / identifier
        try:
            lease = open(root / '.lease', 'rb')
            fcntl.flock(lease, fcntl.LOCK_SH)
        except FileNotFoundError:
            raise HTTPException(404, 'Version indisponible.') from None
        try:
            with store().connect() as db:
                row = db.execute("SELECT 1 FROM snapshots WHERE id=? AND state='ready'", (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Version expirée.')
            requested = PurePosixPath(path or '.')
            if requested.is_absolute() or '..' in requested.parts or any(p in ('.lease','.tardis.json') for p in requested.parts) or '\\' in path:
                raise HTTPException(400, 'Chemin refusé.')
            target = root
            for part in requested.parts:
                target = target / part
                if target.is_symlink():
                    raise HTTPException(400, 'Les liens symboliques ne sont pas suivis.')
            if not target.exists():
                raise HTTPException(404, 'Fichier introuvable.')
            return target, lease
        except BaseException:
            lease.close()
            raise

    @api.get('/api/v1/snapshots/{identifier}/files')
    def files(identifier: str, path: str = Query(default='')):
        target, lease = snapshot_path(identifier, path)
        try:
            if not target.is_dir():
                raise HTTPException(400, 'Un dossier est attendu.')
            result = []
            for entry in sorted(target.iterdir(), key=lambda p:p.name.casefold()):
                if entry.name in ('.lease', '.tardis.json'):
                    continue
                link = entry.is_symlink()
                result.append({'name': entry.name, 'path': str(PurePosixPath(path)/entry.name),
                    'directory': not link and entry.is_dir(), 'symlink': link,
                    'bytes': entry.lstat().st_size})
            return result
        finally:
            lease.close()

    @api.get('/api/v1/snapshots/{identifier}/download')
    def download(identifier: str, path: str):
        target, lease = snapshot_path(identifier, path)
        if not target.is_file():
            lease.close()
            raise HTTPException(400, 'Sélectionner un fichier ordinaire.')
        return FileResponse(target, filename=target.name, media_type='application/octet-stream',
                            background=BackgroundTask(lease.close))
