"""One-shot privileged-to-volumes setup, completed before Web/worker startup."""
import fcntl
import os

from cryptography.hazmat.primitives import serialization

from .config import Settings
from .store import Store
from .vault import Vault, PublicVault, PREFIX


def initialize(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.data_dir/'initialize.lock','a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = Store(settings.data_dir/'tardis.db',settings.database_backup_dir)
        with store.connect() as db:
            keys = db.execute('SELECT ciphertext FROM ssh_keys').fetchall()
            has_keys = bool(keys)
            has_envelopes = any(row[0].startswith(PREFIX) for row in keys)
        if has_envelopes and not (settings.secret_dir/'envelope.pem').exists():
            raise RuntimeError('Clé privée de coffre absente : restaurer le volume de secrets, sans générer une nouvelle identité.')
        vault = Vault(settings.secret_dir,has_keys)
        directory = settings.public_directory
        directory.mkdir(parents=True,exist_ok=True)
        public = vault.private_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                            serialization.PublicFormat.SubjectPublicKeyInfo)
        pending = directory/'envelope.pub.part'
        pending.write_bytes(public); os.chmod(pending,0o644)
        os.replace(pending,directory/'envelope.pub')
        importer = PublicVault(directory)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id,ciphertext FROM ssh_keys').fetchall():
                if not row['ciphertext'].startswith(PREFIX):
                    encrypted = importer.encrypt(vault.decrypt(row['ciphertext']))
                    db.execute('UPDATE ssh_keys SET ciphertext=? WHERE id=?',(encrypted,row['id']))
        return store


if __name__ == '__main__':
    initialize(Settings.from_env())
