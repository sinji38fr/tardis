"""Online SQLite recovery copies. Never copy a live database with shutil.copy."""
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def database_copies(directory):
    result = []
    for path in Path(directory).glob('tardis-*.json'):
        try:
            record = json.loads(path.read_text())
            if (Path(record['filename']).name == record['filename']
                    and (Path(directory) / record['filename']).is_file()):
                result.append(record)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(result, key=lambda x: x['created_at'], reverse=True)


def backup_database(source, directory, keep=30):
    import fcntl
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / '.database.lock', 'a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _backup_database(source, directory, keep)


def _backup_database(source, directory, keep):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    identifier = str(uuid4())
    date = datetime.now(timezone.utc)
    filename = f'tardis-{date:%Y%m%dT%H%M%S}-{identifier}.sqlite3'
    temporary = directory / (filename + '.part')
    target = directory / filename
    deadline = time.monotonic() + 120

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('La copie SQLite a dépassé 120 secondes.')

    try:
        with sqlite3.connect(source, timeout=15) as original:
            with sqlite3.connect(temporary) as copied:
                os.chmod(temporary, 0o600)
                original.backup(copied, pages=128, progress=progress, sleep=0.05)
                if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Contrôle d’intégrité SQLite en échec.')
        # Explicitly close connections: sqlite3's context manager only commits.
        original.close()
        copied.close()
        with open(temporary, 'rb') as handle:
            checksum = hashlib.file_digest(handle, 'sha256').hexdigest()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        record = {'id': identifier, 'filename': filename, 'created_at': date.isoformat(),
                  'bytes': target.stat().st_size, 'sha256': checksum, 'integrity': 'ok'}
        manifest = target.with_suffix('.json')
        pending = manifest.with_suffix('.json.part')
        with open(pending, 'x', encoding='utf-8') as handle:
            os.chmod(pending, 0o600)
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, manifest)
        # Remove old copies only after a complete, verified new copy is published.
        for old in database_copies(directory)[keep:]:
            old_path = directory / old['filename']
            old_path.unlink(missing_ok=True)
            old_path.with_suffix('.json').unlink(missing_ok=True)
        return record
    finally:
        if 'copied' in locals():
            copied.close()
        if 'original' in locals():
            original.close()
        temporary.unlink(missing_ok=True)
