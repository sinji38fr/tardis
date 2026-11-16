import ipaddress
import re
import json
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ServerInput(Input):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    enabled: bool = True

    @field_validator('host')
    @classmethod
    def valid_host(cls, value):
        try:
            ipaddress.ip_address(value)
        except ValueError:
            labels = value.rstrip('.').split('.')
            if not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x) for x in labels):
                raise ValueError('Adresse IP ou nom DNS attendu, sans protocole ni chemin.')
        return value


class KeyInput(Input):
    name: str = Field(min_length=1, max_length=100)
    private_key: SecretStr
    passphrase: SecretStr = SecretStr('')

    @field_validator('private_key', 'passphrase')
    @classmethod
    def bounded_secret(cls, value):
        if len(value.get_secret_value()) > 65536:
            raise ValueError('Secret trop volumineux.')
        return value


class Retention(Input):
    daily: int = Field(default=7, ge=1, le=3650)
    weekly: int = Field(default=4, ge=0, le=520)
    monthly: int = Field(default=12, ge=0, le=120)


class BackupInput(Input):
    name: str = Field(min_length=1, max_length=100)
    server_id: str
    key_id: str
    login: str = Field(pattern=r'^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}\$?$')
    folders: list[str] = Field(min_length=1, max_length=100)
    excludes: list[str] = Field(default_factory=list, max_length=100)
    schedule: str = Field(default='02:00', pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    timezone: str = 'Europe/Paris'
    retention: Retention = Field(default_factory=Retention)
    enabled: bool = True
    engine: str = Field(default='rsnapshot', pattern=r'^[a-z][a-z0-9_-]{0,31}$')
    engine_options: dict = Field(default_factory=dict)
    timeout_seconds: int = Field(default=21600, ge=60, le=86400)

    @field_validator('engine_options')
    @classmethod
    def bounded_options(cls, value):
        if len(json.dumps(value, ensure_ascii=False)) > 16384:
            raise ValueError('Options de moteur trop volumineuses.')
        return value
    @field_validator('folders')
    @classmethod
    def valid_folders(cls, values):
        result = []
        for value in values:
            value = value.strip()
            path = PurePosixPath(value)
            if (not value.startswith('/') or value.startswith('//') or '..' in path.parts
                    or any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 4096):
                raise ValueError('Chaque dossier doit être un chemin Linux absolu, sans traversée ni caractère de contrôle.')
            normalized = str(path)
            for previous in result:
                other = PurePosixPath(previous)
                if path == other or path in other.parents or other in path.parents:
                    raise ValueError('Les dossiers ne peuvent pas être dupliqués ou imbriqués.')
            result.append(normalized)
        return result

    @field_validator('excludes')
    @classmethod
    def valid_excludes(cls, values):
        if any(not x or len(x) > 4096 or any(ord(c) < 32 for c in x) for x in values):
            raise ValueError('Motif d’exclusion invalide.')
        return values

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Fuseau horaire inconnu.') from None
        return value


class HostApproval(Input):
    public_key: str = Field(min_length=20, max_length=16384)
    fingerprint: str = Field(min_length=10, max_length=100)


class RunInput(Input):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class RestoreInput(Input):
    path: str = Field(min_length=1,max_length=4096)
    destination: str = Field(min_length=1,max_length=4096)
    overwrite: bool = False

    @field_validator('destination')
    @classmethod
    def destination_path(cls,value):
        path=PurePosixPath(value)
        if not value.startswith('/') or value.startswith('//') or '..' in path.parts or path==PurePosixPath('/') or any(ord(c)<32 for c in value):
            raise ValueError('Destination absolue hors racine, sans traversée ni caractère de contrôle, requise.')
        return str(path)
