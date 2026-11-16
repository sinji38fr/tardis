import json
import sqlite3

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.vault import Vault

HEADERS = {'X-Tardis-Request': '1'}


@pytest.fixture
def settings(tmp_path):
    return Settings('http://testserver', tmp_path / 'data', tmp_path / 'secrets', tmp_path/'backups', tmp_path/'database-backups')


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def private_key(password=None):
    key = ed25519.Ed25519PrivateKey.generate()
    encrypted = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, encrypted).decode()


def create_resources(client):
    server = client.post('/api/v1/servers', headers=HEADERS,
                         json={'name':'Serveur test','host':'example.local'}).json()
    key = client.post('/api/v1/keys', headers=HEADERS,
                      json={'name':'Clé test','private_key':private_key()}).json()
    return server, key


def backup(server, key, **extra):
    return {'name':'Documents', 'server_id':server['id'], 'key_id':key['id'], 'login':'backup',
            'folders':['/srv/documents','/etc/app'], **extra}


def test_no_login_and_honest_capabilities(client):
    response = client.get('/')
    assert response.status_code == 200
    assert 'fenêtre de login' not in response.text
    assert 'Nouvelle sauvegarde' in response.text
    assert client.get('/static/app.js').status_code == 200
    status = client.get('/api/v1/status').json()
    assert status['authentication'] == 'none'
    assert status['capabilities']['backup_execution'] is True
    assert client.get('/healthz').json()['status'] == 'ok'


def test_backups_have_independent_logins_keys_folders_and_revisions(client):
    server, key = create_resources(client)
    first = client.post('/api/v1/backups', headers=HEADERS, json=backup(server,key))
    assert first.status_code == 201, first.text
    second = client.post('/api/v1/backups', headers=HEADERS,
                         json=backup(server,key,name='Autre',login='alice',folders=['/home/alice']))
    assert second.status_code == 201
    edited = backup(server,key,folders=['/srv/new'])
    changed = client.put('/api/v1/backups/' + first.json()['id'],headers=HEADERS,json=edited)
    assert changed.json()['revision'] == 2
    revisions = client.get('/api/v1/backups/' + first.json()['id'] + '/revisions').json()
    assert revisions[1]['folders'] == ['/srv/documents','/etc/app']
    assert revisions[1]['server']['host'] == 'example.local'
    assert 'ciphertext' not in json.dumps(revisions)
    rows = client.get('/api/v1/backups').json()
    assert next(r for r in rows if r['id']==second.json()['id'])['folders'] == ['/home/alice']
    assert len(client.get('/api/v1/keys').json()[0]['backups']) == 2
    assert client.delete('/api/v1/keys/'+key['id'],headers=HEADERS).status_code == 409
    assert client.delete('/api/v1/servers/'+server['id'],headers=HEADERS).status_code == 409
    assert client.put('/api/v1/servers/'+server['id'],headers=HEADERS,
                      json={'name':'Renommé','host':'different.local'}).status_code == 409


@pytest.mark.parametrize('folders', [['relative'],['/srv','/srv/app'],['/srv/../etc'],['/srv/','/srv'],['/srv\nsecret']])
def test_reject_ambiguous_paths(client, folders):
    server, key = create_resources(client)
    response = client.post('/api/v1/backups',headers=HEADERS,json=backup(server,key,folders=folders))
    assert response.status_code == 422


def test_encrypted_key_file_import_and_no_plaintext(client, settings):
    secret = private_key(b'passphrase-test')
    response = client.post('/api/v1/keys/import',headers=HEADERS,
        data={'name':'Chiffrée','passphrase':'passphrase-test'},files={'file':('key.pem',secret)})
    assert response.status_code == 201, response.text
    assert response.json()['fingerprint'].startswith('SHA256:')
    assert 'private_key' not in response.json()
    assert 'ciphertext' not in client.get('/api/v1/keys').text
    with sqlite3.connect(settings.data_dir / 'tardis.db') as db:
        ciphertext = db.execute('SELECT ciphertext FROM ssh_keys').fetchone()[0]
        assert b'PRIVATE KEY' not in ciphertext
    for file in settings.data_dir.iterdir():
        if file.is_file():
            content = file.read_bytes()
            assert b'passphrase-test' not in content
            assert b'BEGIN OPENSSH PRIVATE KEY' not in content
    # Persisted vault key decrypts the normalized key after application restart.
    with TestClient(create_app(settings)) as restarted:
        plaintext = Vault(settings.secret_dir,has_keys=True,read_only=True).decrypt(ciphertext)
        assert serialization.load_ssh_private_key(plaintext,password=None)
        assert len(restarted.get('/api/v1/keys').json()) == 1


def test_bad_imports_never_echo_secrets(client):
    key = private_key(b'never-echo-this')
    for payload in [
        {'name':'bad','private_key':key,'passphrase':'wrong'},
        {'name':'bad','private_key':'ssh-ed25519 AAAA-only-public'},
        {'name':'','private_key':key,'passphrase':'never-echo-this'},
    ]:
        response = client.post('/api/v1/keys',headers=HEADERS,json=payload)
        assert response.status_code == 422
        assert 'never-echo-this' not in response.text
        assert 'BEGIN OPENSSH PRIVATE KEY' not in response.text
    assert client.get('/api/v1/keys').json() == []


def test_mutations_reject_cross_origin_and_simple_forms(client):
    payload = {'name':'test','host':'example.local'}
    assert client.post('/api/v1/servers',json=payload).status_code == 403
    assert client.post('/api/v1/servers',headers={**HEADERS,'Origin':'https://outside.example'},json=payload).status_code == 403
    assert client.post('/api/v1/servers',headers={**HEADERS,'Origin':'http://testserver'},json=payload).status_code == 201
    assert client.post('/api/v1/keys',headers=HEADERS,content=b'x'*(256*1024+1)).status_code == 413
    assert client.post('/api/v1/servers',headers={**HEADERS,'Host':'attacker.example','Origin':'http://attacker.example'},json=payload).status_code == 400


def test_subpath_assets_routes_and_configured_url(tmp_path):
    settings = Settings('https://backup.example/tardis', tmp_path/'data', tmp_path/'secrets', tmp_path/'backups', tmp_path/'database-backups')
    with TestClient(create_app(settings),base_url='https://backup.example') as client:
        response = client.get('/tardis/')
        assert response.status_code == 200
        assert 'href="/tardis/static/app.css"' in response.text
        assert 'https://backup.example/tardis' in response.text
        assert client.get('/tardis/static/app.js').status_code == 200
        assert client.get('/tardis/api/v1/status').status_code == 200
        assert client.get('/tardis/openapi.json').status_code == 200
        assert client.get('/healthz').status_code == 200
        assert client.post('/tardis/api/v1/servers',headers={**HEADERS,'Origin':'https://backup.example'},
                           json={'name':'test','host':'192.0.2.1'}).status_code == 201


def test_missing_vault_key_does_not_silently_replace_it(client, settings):
    create_resources(client)
    (settings.secret_dir/'master.key').unlink()
    with pytest.raises(RuntimeError, match='Clé maître absente'):
        with TestClient(create_app(settings)):
            pass


def test_unique_names_and_reference_validation(client):
    server,key = create_resources(client)
    assert client.post('/api/v1/servers',headers=HEADERS,json={'name':'SERVEUR TEST','host':'other.local'}).status_code == 409
    assert client.post('/api/v1/backups',headers=HEADERS,json=backup(server,{'id':'missing'})).status_code == 404
    assert client.get('/api/v1/backups/unknown/revisions').status_code == 404
