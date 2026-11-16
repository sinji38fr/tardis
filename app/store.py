import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path

from .maintenance import backup_database


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid4())


class Store:
    def __init__(self, path, recovery_dir=None):
        self.path = str(path)
        # Keep a verified pre-migration copy before touching an existing schema.
        if Path(path).exists():
            with sqlite3.connect(path) as existing:
                version = existing.execute('PRAGMA user_version').fetchone()[0]
            existing.close()
            if version < 3:
                backup_database(self.path, recovery_dir or Path(path).parent / 'recovery')
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS servers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    host TEXT NOT NULL, port INTEGER NOT NULL, enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ssh_keys (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    fingerprint TEXT NOT NULL, key_type TEXT NOT NULL, public_key TEXT NOT NULL,
                    ciphertext BLOB NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS backups (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    server_id TEXT NOT NULL REFERENCES servers(id),
                    key_id TEXT NOT NULL REFERENCES ssh_keys(id),
                    login TEXT NOT NULL, folders TEXT NOT NULL, excludes TEXT NOT NULL,
                    schedule TEXT NOT NULL, timezone TEXT NOT NULL, retention TEXT NOT NULL,
                    enabled INTEGER NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS backup_revisions (
                    backup_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    configuration TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (backup_id, revision));
                CREATE TABLE IF NOT EXISTS audit (
                    id TEXT PRIMARY KEY, event TEXT NOT NULL, resource_id TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS trusted_hosts (
                    server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
                    host TEXT NOT NULL, port INTEGER NOT NULL,
                    public_key TEXT NOT NULL, fingerprint TEXT NOT NULL, approved_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, backup_id TEXT, kind TEXT NOT NULL,
                    state TEXT NOT NULL, trigger TEXT NOT NULL,
                    configuration TEXT NOT NULL, idempotency_key TEXT,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    log TEXT NOT NULL DEFAULT '', error TEXT,
                    UNIQUE(backup_id, idempotency_key));
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, backup_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    engine TEXT NOT NULL, configuration TEXT NOT NULL,
                    created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready');
                CREATE TABLE IF NOT EXISTS worker_state (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS restore_claims (
                    preview_id TEXT PRIMARY KEY REFERENCES jobs(id), job_id TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, event TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL, created_at TEXT NOT NULL, error TEXT);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(backups)')}
            if 'engine' not in columns:
                db.execute("ALTER TABLE backups ADD COLUMN engine TEXT NOT NULL DEFAULT 'rsnapshot'")
            if 'timeout_seconds' not in columns:
                db.execute('ALTER TABLE backups ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 21600')
            if 'engine_options' not in columns:
                db.execute("ALTER TABLE backups ADD COLUMN engine_options TEXT NOT NULL DEFAULT '{}'")
            job_columns = {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
            if 'result' not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN result TEXT NOT NULL DEFAULT '{}'")
            db.execute('PRAGMA user_version=3')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def audit(db, event, resource_id):
        db.execute('INSERT INTO audit VALUES (?,?,?,?)', (uid(), event, resource_id, now()))

    @staticmethod
    def revision(db, record):
        db.execute('INSERT INTO backup_revisions VALUES (?,?,?,?)',
                   (record['id'], record['revision'], json.dumps(record, ensure_ascii=False), now()))
