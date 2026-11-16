import json
import sqlite3

from .store import now, uid


def enqueue(store, backup_id=None, kind='backup', trigger='manual', idempotency_key=None, registry=None, operation=None, frozen=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        configuration = {}
        if backup_id:
            row = db.execute('SELECT * FROM backups WHERE id=?', (backup_id,)).fetchone()
            if row is None:
                raise LookupError('Sauvegarde introuvable.')
            if idempotency_key:
                existing = db.execute('SELECT * FROM jobs WHERE backup_id=? AND idempotency_key=?',
                                      (backup_id, idempotency_key)).fetchone()
                if existing:
                    return dict(existing)
            if db.execute("SELECT 1 FROM jobs WHERE backup_id=? AND state IN ('queued','running')", (backup_id,)).fetchone():
                raise ValueError('Une opération est déjà en attente ou en cours pour cette sauvegarde.')
            server = db.execute('SELECT * FROM servers WHERE id=?', (row['server_id'],)).fetchone()
            if (kind in ('backup','test') and not row['enabled']) or not server['enabled']:
                raise ValueError('Sauvegarde ou serveur désactivé.')
            trust = db.execute('SELECT * FROM trusted_hosts WHERE server_id=? AND host=? AND port=?',
                               (server['id'], server['host'], server['port'])).fetchone()
            if trust is None:
                raise ValueError('Valider l’empreinte SSH du serveur avant de lancer une opération.')
            configuration = dict(row)
            for field in ('folders', 'excludes', 'retention', 'engine_options'):
                configuration[field] = json.loads(configuration[field])
            configuration['server'] = dict(server)
            configuration['trusted_host'] = dict(trust)
            if registry:
                registry.get(configuration['engine'])
            if frozen is not None:
                if configuration['trusted_host']['fingerprint'] != frozen['trusted_host']['fingerprint']:
                    raise ValueError('Clé d’hôte modifiée : refaire l’aperçu.')
                if not db.execute('SELECT 1 FROM ssh_keys WHERE id=?',(frozen['key_id'],)).fetchone():
                    raise ValueError('Clé SSH indisponible : refaire l’aperçu.')
                configuration=frozen.copy()
            if operation:
                configuration['operation']=operation
        elif db.execute("SELECT 1 FROM jobs WHERE kind='database' AND state IN ('queued','running')").fetchone():
            raise ValueError('Une copie SQLite est déjà en attente ou en cours.')
        identifier = uid()
        db.execute('''INSERT INTO jobs (id,backup_id,kind,state,trigger,configuration,idempotency_key,created_at)
                      VALUES (?,?,?,'queued',?,?,?,?)''',
                   (identifier, backup_id, kind, trigger, json.dumps(configuration), idempotency_key, now()))
        store.audit(db, f'{kind}.queued', identifier)
        return dict(db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())


def public_job(row, include_log=True):
    result = dict(row)
    configuration = json.loads(result.pop('configuration'))
    result['backup_name'] = configuration.get('name', 'Base SQLite TARDIS')
    result['revision'] = configuration.get('revision')
    result['engine'] = configuration.get('engine')
    result['result'] = json.loads(result.get('result') or '{}')
    if not include_log:
        result.pop('log', None)
    return result
