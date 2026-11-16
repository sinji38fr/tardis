import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    public_url: str
    data_dir: Path
    secret_dir: Path
    backup_dir: Path = Path('backups')
    database_backup_dir: Path = Path('database-backups')
    database_backup_interval: int = 3600
    database_backup_keep: int = 30
    public_key_dir: Path | None = None
    auto_initialize: bool = True
    webhook_url: str = ''
    webhook_secret: str = ''

    def __post_init__(self):
        url = urlsplit(self.public_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                or url.password or url.query or url.fragment or '//' in url.path
                or any(p in ('.', '..') for p in url.path.split('/'))):
            raise ValueError('TARDIS_PUBLIC_URL doit être une URL HTTP(S) absolue, sans identifiants ni paramètres.')
        # Validate the port too.
        url.port
        if self.database_backup_interval < 60 or self.database_backup_keep < 1:
            raise ValueError('Intervalle SQLite minimum : 60 secondes ; rétention minimum : 1.')
        if self.webhook_url:
            hook = urlsplit(self.webhook_url)
            if hook.scheme not in ('http','https') or not hook.hostname or hook.username or hook.password or hook.fragment:
                raise ValueError('URL de webhook HTTP(S) invalide.')

    @property
    def public_directory(self):
        return self.public_key_dir or self.data_dir / 'public-keys'

    @property
    def prefix(self):
        return urlsplit(self.public_url).path.rstrip('/')

    @property
    def origin(self):
        url = urlsplit(self.public_url)
        return f'{url.scheme}://{url.netloc}'

    @classmethod
    def from_env(cls):
        return cls(os.getenv('TARDIS_PUBLIC_URL', 'http://localhost:8088').rstrip('/'),
                   Path(os.getenv('TARDIS_DATA_DIR', 'data')),
                   Path(os.getenv('TARDIS_SECRET_DIR', 'secrets')),
                   Path(os.getenv('TARDIS_BACKUP_DIR', 'backups')),
                   Path(os.getenv('TARDIS_DATABASE_BACKUP_DIR', 'database-backups')),
                   int(os.getenv('TARDIS_DATABASE_BACKUP_INTERVAL', '3600')),
                   int(os.getenv('TARDIS_DATABASE_BACKUP_KEEP', '30')),
                   Path(os.environ['TARDIS_PUBLIC_KEY_DIR']) if os.getenv('TARDIS_PUBLIC_KEY_DIR') else None,
                   os.getenv('TARDIS_AUTO_INITIALIZE','1') == '1',
                   os.getenv('TARDIS_WEBHOOK_URL',''), os.getenv('TARDIS_WEBHOOK_SECRET',''))
