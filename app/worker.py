"""Single durable worker: database protection, scheduling and engine-independent jobs."""
import fcntl
import json
import hashlib
import logging
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .engines import CaptureContext, default_registry
from .maintenance import backup_database, database_copies
from .queue import enqueue
from .ssh import ssh_options, known_host_line
from .store import Store, now
from .vault import Vault
from .storage import measure_storage, remove_tree
from .notifications import emit, deliver_pending
from .operations import restore_operation, verify_operation

LOG = logging.getLogger('tardis.worker')


def retained_ids(rows, policy, zone):
    """Rolling latest runs + last version of each observed week and month."""
    ordered = sorted(rows, key=lambda r: r['created_at'], reverse=True)
    keep = {row['id'] for row in ordered[:policy['daily']]}
    weeks, months = {}, {}
    for row in ordered:
        local = datetime.fromisoformat(row['created_at']).astimezone(ZoneInfo(zone))
        weeks.setdefault(local.isocalendar()[:2], row['id'])
        months.setdefault((local.year, local.month), row['id'])
    keep.update(list(weeks.values())[:policy['weekly']])
    keep.update(list(months.values())[:policy['monthly']])
    return keep


class Worker:
    def __init__(self, settings=None, registry=None):
        self.settings = settings or Settings.from_env()
        self.registry = registry or default_registry()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.backup_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.settings.data_dir / 'tardis.db', self.settings.database_backup_dir)
        with self.store.connect() as db:
            has_keys = db.execute('SELECT count(*) FROM ssh_keys').fetchone()[0] > 0
        self.vault = Vault(self.settings.secret_dir, has_keys, read_only=True)
        self.stop = threading.Event()
        self.lock = None

    def acquire(self):
        self.lock = open(self.settings.data_dir / 'worker.lock', 'a+')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('Un worker TARDIS est déjà actif pour cette base.') from None

    def close(self):
        if self.lock:
            self.lock.close()
            self.lock = None

    def heartbeat(self):
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO worker_state VALUES ('heartbeat',?)", (now(),))

    def append_log(self, identifier, message):
        # Bounded persistent log, no command arguments or key material.
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET log=substr(log || ?, -131072) WHERE id=?',
                       (f'[{now()}] {message}\n', identifier))

    def runner(self, job):
        configuration = json.loads(job['configuration'])
        deadline = time.monotonic() + configuration.get('timeout_seconds', 21600)
        last_check = 0.0

        def checkpoint():
            nonlocal last_check
            moment = time.monotonic()
            if self.stop.is_set():
                raise InterruptedError('Opération interrompue.')
            if moment > deadline:
                raise TimeoutError('Délai maximal de sauvegarde dépassé.')
            if moment - last_check < 0.25:
                return
            last_check = moment
            with self.store.connect() as db:
                state = db.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
            if state == 'cancelled':
                raise InterruptedError('Opération interrompue.')

        def run(command,log_output=True):
            checkpoint()
            self.append_log(job['id'], f"Étape : {Path(command[0]).name} {command[-1] if command[0].endswith('rsnapshot') else ''}")
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
                try:
                    while process.poll() is None:
                        checkpoint()
                        self.heartbeat()
                        self.stop.wait(0.25)
                    output.seek(0, os.SEEK_END)
                    size = output.tell()
                    output.seek(0)
                    run.last_digest=hashlib.file_digest(output,'sha256').hexdigest()
                    output.seek(max(0, size - 32768))
                    text = output.read().decode('utf-8', errors='replace')
                    if text and log_output:
                        self.append_log(job['id'], text)
                    if process.returncode != 0:
                        raise RuntimeError(f'{Path(command[0]).name} a échoué (code {process.returncode}). Consulter le journal.')
                    return text
                finally:
                    # Kill descendants too, even if the direct child already exited.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
        run.checkpoint = checkpoint
        return run

    def recover(self):
        """Finalize a fully published tree after a crash; never publish partial work."""
        with self.store.connect() as db:
            running = list(db.execute("SELECT * FROM jobs WHERE state='running'"))
        for job in running:
            directory = self.settings.backup_dir / 'snapshots' / job['id']
            manifest = directory / '.tardis.json'
            if job['kind'] == 'backup' and manifest.is_file():
                record = json.loads(manifest.read_text())
                with self.store.connect() as db:
                    self.publish_record(db, job, record['created_at'])
            else:
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET state='failed',finished_at=?,error=? WHERE id=?",
                               (now(), 'Worker interrompu ; relancer l’opération.', job['id']))

    def schedule(self, moment=None):
        moment = moment or datetime.now(timezone.utc)
        with self.store.connect() as db:
            rows = list(db.execute('''SELECT b.* FROM backups b JOIN servers s ON s.id=b.server_id
                JOIN trusted_hosts t ON t.server_id=s.id AND t.host=s.host AND t.port=s.port
                WHERE b.enabled=1 AND s.enabled=1'''))
        for row in rows:
            local = moment.astimezone(ZoneInfo(row['timezone']))
            if local.strftime('%H:%M') < row['schedule']:
                continue
            try:
                enqueue(self.store, row['id'], trigger='schedule',
                        idempotency_key='schedule:' + local.date().isoformat(), registry=self.registry)
            except (ValueError, LookupError):
                pass

    def schedule_database(self):
        copies = database_copies(self.settings.database_backup_dir)
        age = float('inf') if not copies else (datetime.now(timezone.utc) - datetime.fromisoformat(copies[0]['created_at'])).total_seconds()
        if age >= self.settings.database_backup_interval:
            # Back off one minute after a failure, rather than creating jobs on every tick.
            with self.store.connect() as db:
                last = db.execute("SELECT finished_at FROM jobs WHERE kind='database' AND state='failed' ORDER BY created_at DESC LIMIT 1").fetchone()
            if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last[0])).total_seconds() < 60:
                return
            try:
                enqueue(self.store, kind='database', trigger='automatic')
            except ValueError:
                pass

    def claim(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY kind='database' DESC,created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running',started_at=? WHERE id=?", (now(), row['id']))
            return dict(row)

    def publish_record(self, db, job, created_at):
        configuration = json.loads(job['configuration'])
        db.execute('''INSERT OR IGNORE INTO snapshots (id,backup_id,job_id,engine,configuration,created_at)
                      VALUES (?,?,?,?,?,?)''', (job['id'], job['backup_id'], job['id'],
                      configuration['engine'], job['configuration'], created_at))
        db.execute("UPDATE jobs SET state='succeeded',finished_at=?,error=NULL WHERE id=?", (now(), job['id']))
        self.store.audit(db, 'backup.succeeded', job['id'])

    def capture(self, job, configuration, options, run):
        root = self.settings.backup_dir
        usage = shutil.disk_usage(root)
        if usage.used / usage.total >= 0.95:
            raise RuntimeError('Stockage occupé à 95 % : collecte refusée sans supprimer de versions.')
        staging = root / 'staging' / job['id']
        destination = root / 'snapshots' / job['id']
        staging.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
        context = CaptureContext(configuration, root / 'work' / configuration['id'], options, run, run.checkpoint)
        self.registry.get(configuration['engine']).capture(context, staging)
        with self.store.connect() as db:
            if db.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'cancelled':
                raise InterruptedError('Opération annulée avant publication.')
        created_at = now()
        (staging / '.lease').touch(mode=0o600)
        manifest = staging / '.tardis.json'
        manifest.write_text(json.dumps({'job_id': job['id'], 'created_at': created_at,
                                        'configuration': configuration}), encoding='utf-8')
        os.chmod(manifest, 0o600)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'cancelled':
                raise InterruptedError('Opération annulée avant publication.')
            os.replace(staging, destination)
            self.publish_record(db, job, created_at)
        try:
            self.prune(configuration)
            self.cleanup_caches()
            self.update_storage()
        except Exception:
            self.append_log(job['id'], 'Version publiée ; nettoyage de rétention incomplet, nouvelle tentative à la prochaine collecte.')

    def prune(self, configuration):
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM snapshots WHERE backup_id=? AND state IN ('ready','deleting')", (configuration['id'],))]
        keep = retained_ids([r for r in rows if r['state']=='ready'], configuration['retention'], configuration['timezone'])
        cutoff=(datetime.now(timezone.utc)-timedelta(minutes=15)).isoformat()
        with self.store.connect() as db:
            pinned=db.execute("SELECT configuration FROM jobs WHERE state IN ('queued','running') OR (kind='restore_preview' AND state='succeeded' AND finished_at>?)",(cutoff,)).fetchall()
        keep.update(json.loads(row[0]).get('operation',{}).get('snapshot_id') for row in pinned)
        for row in rows:
            if row['id'] in keep:
                continue
            directory = self.settings.backup_dir / 'snapshots' / row['id']
            lease = None
            try:
                if directory.exists():
                    lease = open(directory / '.lease', 'rb')
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.store.connect() as db:
                    db.execute("UPDATE snapshots SET state='deleting' WHERE id=?", (row['id'],))
                if directory.exists():
                    remove_tree(directory)
                with self.store.connect() as db:
                    db.execute("UPDATE snapshots SET state='deleted' WHERE id=?", (row['id'],))
                (self.settings.backup_dir/'integrity'/f"{row['id']}.json").unlink(missing_ok=True)
            except BlockingIOError:
                continue  # A download holds a shared lease. Retry on the next run.
            finally:
                if lease:
                    lease.close()

    def execute(self, job):
        configuration = json.loads(job['configuration'])
        result={}
        try:
            self.append_log(job['id'], 'Démarrage de l’opération.')
            if job['kind'] == 'database':
                record = backup_database(self.store.path, self.settings.database_backup_dir,
                                         self.settings.database_backup_keep)
                self.append_log(job['id'], f"Copie SQLite vérifiée : {record['filename']}")
                result=record
            elif job['kind']=='verify':
                configuration['operation']['job_id']=job['id']
                result=verify_operation(self,configuration,self.runner(job))
            else:
                with self.store.connect() as db:
                    key = db.execute('SELECT ciphertext FROM ssh_keys WHERE id=?', (configuration['key_id'],)).fetchone()
                if key is None:
                    raise RuntimeError('La clé SSH requise n’est plus disponible.')
                with tempfile.TemporaryDirectory(prefix='tardis-') as folder:
                    identity = Path(folder) / 'identity'
                    identity.write_bytes(self.vault.decrypt(key[0]))
                    os.chmod(identity, 0o600)
                    known = Path(folder) / 'known_hosts'
                    server = configuration['server']
                    trust = configuration['trusted_host']
                    known.write_text(known_host_line(server, trust['public_key']))
                    options = ssh_options(server, identity, known)
                    run = self.runner(job)
                    target = f"{configuration['login']}@{server['host']}"
                    checks = ['true']
                    for path in configuration['folders'] if job['kind'] in ('backup','test') else []:
                        quoted = shlex.quote(path)
                        checks.append(f'test -d {quoted} && test -r {quoted} && test -x {quoted}')
                    run(['/usr/bin/ssh', *options, target, ' && '.join(checks)])
                    if job['kind'] == 'backup':
                        self.capture(job, configuration, options, run)
                    elif job['kind'] in ('restore','restore_preview'):
                        result=restore_operation(self,job,configuration,options,run)
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='succeeded',finished_at=? WHERE id=? AND state='running'", (now(), job['id']))
                db.execute('UPDATE jobs SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),job['id']))
                self.store.audit(db,f"{job['kind']}.succeeded",job['id'])
            if job['kind']!='restore_preview':emit(self.store,self.settings,f"{job['kind']}.succeeded",job['id'],{'name':configuration.get('name'),'result':result.get('status','ok')})
        except Exception as error:
            published = self.settings.backup_dir / 'snapshots' / job['id'] / '.tardis.json'
            if published.is_file():
                # Files are complete: leave a running job for transactional recovery
                # rather than label a published version as a failed transfer.
                self.append_log(job['id'], 'Publication complète ; finalisation du catalogue au prochain cycle.')
                return
            message = str(error) if isinstance(error, (RuntimeError, ValueError, TimeoutError, InterruptedError, OSError)) else 'Erreur interne du moteur ; opération non publiée.'
            self.append_log(job['id'], message)
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='failed',finished_at=?,error=? WHERE id=? AND state='running'",
                           (now(), message, job['id']))
                self.store.audit(db, f"{job['kind']}.failed", job['id'])
            emit(self.store,self.settings,f"{job['kind']}.failed",job['id'],{'name':configuration.get('name')})
            staging = self.settings.backup_dir / 'staging' / job['id']
            if staging.exists():
                shutil.rmtree(staging)

    def tick(self, auto_database=True):
        self.heartbeat()
        self.recover()
        if auto_database:
            self.schedule_database()
        self.schedule()
        job = self.claim()
        if job:
            self.execute(job)
        return job

    def serve(self):
        self.acquire()
        try:
            self.recover()
            maintenance = threading.Thread(target=self.maintenance_loop, daemon=True)
            maintenance.start()
            pulse = threading.Thread(target=self.heartbeat_loop, daemon=True)
            pulse.start()
            while not self.stop.is_set():
                try:
                    self.tick(auto_database=False)
                except Exception:
                    LOG.exception('Erreur de cycle worker')
                self.stop.wait(2)
        finally:
            self.stop.set()
            if 'maintenance' in locals():
                maintenance.join(timeout=125)
            if 'pulse' in locals():
                pulse.join(timeout=6)
            self.close()

    def heartbeat_loop(self):
        while not self.stop.is_set():
            try:
                self.heartbeat()
            except Exception:
                LOG.exception('Échec de mise à jour du heartbeat')
            self.stop.wait(5)

    def maintenance_loop(self):
        # Database protection remains on time while a remote transfer runs for hours.
        first = True
        last_storage = 0
        while not self.stop.is_set():
            copies = database_copies(self.settings.database_backup_dir)
            age = float('inf') if not copies else (datetime.now(timezone.utc) - datetime.fromisoformat(copies[0]['created_at'])).total_seconds()
            if first or age >= self.settings.database_backup_interval:
                try:
                    record = backup_database(self.store.path, self.settings.database_backup_dir,
                                             self.settings.database_backup_keep)
                    with self.store.connect() as db:
                        db.execute("INSERT OR REPLACE INTO worker_state VALUES ('database_error','')")
                        self.store.audit(db, 'database.succeeded', record['id'])
                    first = False
                except Exception:
                    with self.store.connect() as db:
                        db.execute("INSERT OR REPLACE INTO worker_state VALUES ('database_error',?)",
                                   ('Échec de la copie SQLite ; vérifier le stockage et les permissions.',))
                    LOG.exception('Échec de la copie SQLite automatique')
            try:
                if time.monotonic()-last_storage>=300:
                    self.update_storage()
                    self.cleanup_caches()
                    last_storage=time.monotonic()
                deliver_pending(self.store,self.settings)
            except Exception:
                LOG.exception('Erreur de maintenance de stockage/notifications')
            self.stop.wait(60)

    def update_storage(self):
        record=measure_storage(self.settings.backup_dir)
        record['measured_at']=now()
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO worker_state VALUES ('storage',?)",(json.dumps(record),))
            last=db.execute("SELECT value FROM worker_state WHERE name='storage_alert'").fetchone()
            due=not last or (datetime.now(timezone.utc)-datetime.fromisoformat(last[0])).total_seconds()>21600
            if record['filesystem_used_percent']>=80 and due:
                db.execute("INSERT OR REPLACE INTO worker_state VALUES ('storage_alert',?)",(now(),))
            else:due=False
        if due:emit(self.store,self.settings,'storage.warning','storage',{'used_percent':record['filesystem_used_percent']})

    def cleanup_caches(self):
        with self.store.connect() as db:
            backups=[dict(row) for row in db.execute('SELECT id,engine,revision FROM backups')]
            active=[json.loads(row[0]) for row in db.execute("SELECT configuration FROM jobs WHERE state IN ('queued','running')")]
        for backup in backups:
            keep={backup['revision']}
            keep.update(c['revision'] for c in active if c.get('id')==backup['id'])
            engine=self.registry.get(backup['engine'])
            cleanup=getattr(engine,'cleanup',None)
            if cleanup:
                cleanup(self.settings.backup_dir/'work'/backup['id'],keep)


def main():
    logging.basicConfig(level=logging.INFO)
    worker = Worker()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.serve()


if __name__ == '__main__':
    main()
