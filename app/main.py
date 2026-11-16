import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .models import BackupInput, KeyInput, ServerInput
from .store import Store, now, uid
from .vault import PublicVault
from .initialize import initialize
from .engines import default_registry
from .runtime_api import install_runtime, runtime_status

ASSETS = Path(__file__).parent
KEY_COLUMNS = 'id,name,fingerprint,key_type,public_key,created_at'


def backup_record(row):
    result = dict(row)
    for key in ('folders', 'excludes', 'retention', 'engine_options'):
        result[key] = json.loads(result[key])
    result['enabled'] = bool(result['enabled'])
    return result


def create_app(settings=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        application.state.store = (initialize(settings) if settings.auto_initialize
                                   else Store(settings.data_dir / 'tardis.db', settings.database_backup_dir))
        application.state.vault = PublicVault(settings.public_directory)
        yield

    api = FastAPI(title='TARDIS', version='1.0.0', lifespan=lifespan)
    api.state.settings = settings
    templates = Jinja2Templates(directory=ASSETS / 'templates')
    api.mount('/static', StaticFiles(directory=ASSETS / 'static'), name='static')

    @api.middleware('http')
    async def protect_requests(request: Request, call_next):
        allowed_hosts = {urlsplit(settings.public_url).hostname, 'localhost', '127.0.0.1', '::1'}
        if request.url.hostname not in allowed_hosts:
            return JSONResponse({'detail': 'Hôte refusé : configurer TARDIS_PUBLIC_URL pour cette adresse.'}, status_code=400)
        # No login, but cross-site browser requests must not mutate local configuration.
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('x-tardis-request') != '1':
                return JSONResponse({'detail': 'En-tête X-Tardis-Request: 1 requis.'}, status_code=403)
            origin = request.headers.get('origin')
            actual = f'{request.url.scheme}://{request.url.netloc}'
            if origin and origin not in (settings.origin, actual):
                return JSONResponse({'detail': 'Origine refusée.'}, status_code=403)
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 256 * 1024:
                    return JSONResponse({'detail': 'Requête trop volumineuse (256 Kio maximum).'}, status_code=413)
                chunks.append(chunk)
            request._body = b''.join(chunks)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request.url.path.endswith(('/docs', '/redoc')):
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; object-src 'none'; frame-ancestors 'none'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo a submitted private key/passphrase through Pydantic's input field.
        errors = [{'field': '.'.join(str(p) for p in e['loc'][1:]), 'message': e['msg']} for e in exc.errors()]
        return JSONResponse({'detail': 'Données invalides.', 'errors': errors}, status_code=422)

    @api.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse({'detail': 'Nom déjà utilisé ou élément encore référencé.'}, status_code=409)

    def store():
        return api.state.store

    def require(db, table, identifier):
        # table only comes from constants in this module.
        row = db.execute(f'SELECT * FROM {table} WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Élément introuvable.')
        return row

    @api.get('/healthz')
    def health():
        with store().connect() as db:
            db.execute('SELECT 1').fetchone()
        return {'status': 'ok', 'version': '1.0.0'}

    @api.get('/', response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(request=request, name='index.html',
                                          context={'prefix': settings.prefix, 'public_url': settings.public_url})

    @api.get('/api/v1/status')
    def status():
        with store().connect() as db:
            counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                      for table in ('servers', 'backups', 'ssh_keys')}
        return {'version': '1.0.0', 'public_url': settings.public_url, 'authentication': 'none',
                'counts': counts, 'capabilities': {'configuration': True, 'backup_execution': True,
                                                 'ssh_test': True, 'restore': 'preview_and_remote', 'checksums': True},
                **runtime_status(store(), settings)}

    @api.get('/api/v1/servers')
    def servers():
        with store().connect() as db:
            return [{**dict(row), 'enabled': bool(row['enabled'])}
                    for row in db.execute('''SELECT s.*,t.fingerprint AS host_fingerprint FROM servers s
                        LEFT JOIN trusted_hosts t ON t.server_id=s.id AND t.host=s.host AND t.port=s.port
                        ORDER BY s.name COLLATE NOCASE''')]

    @api.post('/api/v1/servers', status_code=201)
    def add_server(data: ServerInput):
        record = {'id': uid(), **data.model_dump(), 'created_at': now()}
        with store().connect() as db:
            db.execute('INSERT INTO servers VALUES (:id,:name,:host,:port,:enabled,:created_at)', record)
            store().audit(db, 'server.created', record['id'])
        return record

    @api.put('/api/v1/servers/{identifier}')
    def update_server(identifier: str, data: ServerInput):
        with store().connect() as db:
            previous = require(db, 'servers', identifier)
            # Changing an endpoint silently would change every backup attached to it.
            attached = db.execute('SELECT 1 FROM backups WHERE server_id=?', (identifier,)).fetchone()
            if attached and (previous['host'] != data.host or previous['port'] != data.port):
                raise HTTPException(409, 'Serveur utilisé : créer un autre serveur pour changer la destination SSH.')
            record = {**dict(previous), **data.model_dump()}
            db.execute('UPDATE servers SET name=:name,host=:host,port=:port,enabled=:enabled WHERE id=:id', record)
            store().audit(db, 'server.updated', identifier)
        return record

    @api.delete('/api/v1/servers/{identifier}', status_code=204)
    def delete_server(identifier: str):
        with store().connect() as db:
            require(db, 'servers', identifier)
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') AND json_extract(configuration,'$.server_id')=?", (identifier,)).fetchone():
                raise HTTPException(409, 'Serveur utilisé par une opération en attente ou en cours.')
            db.execute('DELETE FROM servers WHERE id=?', (identifier,))
            store().audit(db, 'server.deleted', identifier)

    @api.get('/api/v1/keys')
    def keys():
        with store().connect() as db:
            result = []
            for row in db.execute(f'SELECT {KEY_COLUMNS} FROM ssh_keys ORDER BY name COLLATE NOCASE'):
                record = dict(row)
                record['backups'] = [dict(b) for b in db.execute('SELECT id,name FROM backups WHERE key_id=?', (row['id'],))]
                result.append(record)
            return result

    def save_key(name, private_key, passphrase):
        try:
            material = api.state.vault.import_key(private_key, passphrase)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        record = {'id': uid(), 'name': name, **material, 'created_at': now()}
        with store().connect() as db:
            db.execute('INSERT INTO ssh_keys VALUES (:id,:name,:fingerprint,:key_type,:public_key,:ciphertext,:created_at)', record)
            store().audit(db, 'key.imported', record['id'])
        return {key: record[key] for key in KEY_COLUMNS.split(',')}

    @api.post('/api/v1/keys', status_code=201)
    def add_key(data: KeyInput):
        return save_key(data.name, data.private_key.get_secret_value(), data.passphrase.get_secret_value())

    @api.post('/api/v1/keys/import', status_code=201)
    async def import_file(name: str = Form(min_length=1, max_length=100),
                          file: UploadFile = File(), passphrase: str = Form(default='')):
        try:
            raw = await file.read(65537)
            if len(raw) > 65536:
                raise HTTPException(413, 'Clé trop volumineuse (64 Kio maximum).')
            data = KeyInput(name=name, private_key=raw.decode('utf-8'), passphrase=passphrase)
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(422, 'Fichier de clé ou métadonnées invalides.') from None
        finally:
            await file.close()
        return save_key(data.name, data.private_key.get_secret_value(), data.passphrase.get_secret_value())

    @api.delete('/api/v1/keys/{identifier}', status_code=204)
    def delete_key(identifier: str):
        with store().connect() as db:
            require(db, 'ssh_keys', identifier)
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') AND json_extract(configuration,'$.key_id')=?", (identifier,)).fetchone():
                raise HTTPException(409, 'Clé utilisée par une opération en attente ou en cours.')
            db.execute('DELETE FROM ssh_keys WHERE id=?', (identifier,))
            store().audit(db, 'key.deleted', identifier)

    @api.get('/api/v1/backups')
    def backups():
        with store().connect() as db:
            return [backup_record(row) for row in db.execute('SELECT * FROM backups ORDER BY name COLLATE NOCASE')]

    def save_backup(identifier, data, create):
        try:
            default_registry().validate_options(data.engine, data.engine_options)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        with store().connect() as db:
            # Serialize writers before reading a revision number.
            db.execute('BEGIN IMMEDIATE')
            server = require(db, 'servers', data.server_id)
            key = require(db, 'ssh_keys', data.key_id)
            previous = None if create else require(db, 'backups', identifier)
            record = {'id': identifier, **data.model_dump(),
                      'revision': 1 if create else previous['revision'] + 1,
                      'created_at': now() if create else previous['created_at'], 'updated_at': now()}
            encoded = {**record, **{k: json.dumps(record[k], ensure_ascii=False) for k in ('folders', 'excludes', 'retention', 'engine_options')}}
            if create:
                db.execute('''INSERT INTO backups
                    (id,name,server_id,key_id,login,folders,excludes,schedule,timezone,retention,enabled,
                     revision,created_at,updated_at,engine,timeout_seconds,engine_options) VALUES
                    (:id,:name,:server_id,:key_id,:login,:folders,:excludes,:schedule,:timezone,:retention,
                     :enabled,:revision,:created_at,:updated_at,:engine,:timeout_seconds,:engine_options)''', encoded)
            else:
                db.execute('''UPDATE backups SET name=:name,server_id=:server_id,key_id=:key_id,
                    login=:login,folders=:folders,excludes=:excludes,schedule=:schedule,timezone=:timezone,
                    retention=:retention,enabled=:enabled,revision=:revision,updated_at=:updated_at,
                    engine=:engine,timeout_seconds=:timeout_seconds,engine_options=:engine_options WHERE id=:id''', encoded)
            store().revision(db, {**record, 'server': dict(server),
                                  'key': {k: key[k] for k in KEY_COLUMNS.split(',')}})
            store().audit(db, 'backup.created' if create else 'backup.updated', identifier)
        return record

    @api.post('/api/v1/backups', status_code=201)
    def add_backup(data: BackupInput):
        return save_backup(uid(), data, True)

    @api.put('/api/v1/backups/{identifier}')
    def update_backup(identifier: str, data: BackupInput):
        return save_backup(identifier, data, False)

    @api.get('/api/v1/backups/{identifier}/revisions')
    def revisions(identifier: str):
        with store().connect() as db:
            require(db, 'backups', identifier)
            return [json.loads(row[0]) for row in db.execute(
                'SELECT configuration FROM backup_revisions WHERE backup_id=? ORDER BY revision DESC', (identifier,))]

    @api.get('/api/v1/audit')
    def audit():
        with store().connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM audit ORDER BY created_at DESC LIMIT 100')]

    install_runtime(api, settings)
    if not settings.prefix:
        return api
    # Incoming requests keep the configured prefix; do not strip it in the reverse proxy.
    outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    outer.state = api.state

    @outer.get('/healthz')
    def outer_health():
        return health()

    outer.mount(settings.prefix, api)
    return outer


app = create_app()
