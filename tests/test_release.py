import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.initialize import initialize
from app.vault import Vault,PREFIX
from app.storage import measure_storage
from app.notifications import emit,deliver_pending
from app.worker import Worker
from app.engines.rsnapshot import RsnapshotEngine
from app.store import now
from test_runtime import environment,ssh_server,setup_backup,run_job
from test_api import private_key


def test_web_can_import_without_mounting_any_private_vault(environment):
    settings,client=environment
    web_settings=replace(settings,secret_dir=Path('/unmounted-private-vault'),auto_initialize=False,
                         public_key_dir=settings.public_directory)
    with TestClient(create_app(web_settings)) as web:
        assert not hasattr(web.app.state.vault,'decrypt')
        response=web.post('/api/v1/keys',headers={'X-Tardis-Request':'1'},json={'name':'Imported without private mount','private_key':private_key()})
        assert response.status_code==201,response.text
    with sqlite3.connect(settings.data_dir/'tardis.db') as db:
        ciphertext=db.execute('SELECT ciphertext FROM ssh_keys').fetchone()[0]
    assert ciphertext.startswith(PREFIX)
    assert b'PRIVATE KEY' in Vault(settings.secret_dir,True,True).decrypt(ciphertext)


def test_legacy_keys_are_migrated_without_changing_identity(environment):
    settings,client=environment
    vault=Vault(settings.secret_dir,False)
    old=vault.import_key(private_key(),'')
    with client.app.state.store.connect() as db:
        db.execute('INSERT INTO ssh_keys VALUES (?,?,?,?,?,?,?)',('legacy','Legacy',old['fingerprint'],old['key_type'],old['public_key'],old['ciphertext'],now()))
    initialize(settings)
    with client.app.state.store.connect() as db:
        row=db.execute("SELECT * FROM ssh_keys WHERE id='legacy'").fetchone()
    assert row['ciphertext'].startswith(PREFIX)
    assert row['fingerprint']==old['fingerprint']
    assert vault.decrypt(row['ciphertext'])==vault.cipher.decrypt(old['ciphertext'])


def test_physical_storage_deduplicates_hardlinks(tmp_path):
    root=tmp_path/'storage';root.mkdir()
    source=root/'a';source.write_bytes(b'x'*65536)
    os.link(source,root/'b')
    measured=measure_storage(root)
    assert measured['logical_bytes']==131072
    assert measured['file_entries']==2
    assert measured['allocated_bytes']<measured['logical_bytes']
    assert measured['unique_inodes']==2 # directory + a/b inode


def test_restore_preview_confirm_no_clobber_and_checksum(environment,ssh_server,tmp_path):
    settings,client=environment
    source=tmp_path/'original';source.mkdir();(source/'a.txt').write_text('restore me')
    backup,_=setup_backup(client,ssh_server,[source])
    worker=Worker(settings);worker.acquire()
    headers={'X-Tardis-Request':'1'}
    try:
        captured=run_job(client,worker,backup,'capture')
        assert captured['state']=='succeeded',captured['log']
        snapshot=captured['id'];root=settings.backup_dir/'snapshots'/snapshot
        selected=next(root.rglob('a.txt'));relative=str(selected.relative_to(root))
        # First content control creates a baseline, the next verifies it.
        baseline=client.post(f'/api/v1/snapshots/{snapshot}/verify',headers=headers)
        assert baseline.status_code==202,baseline.text
        worker.execute(worker.claim())
        assert client.get('/api/v1/jobs/'+baseline.json()['id']).json()['result']['status']=='reference_created'
        verified=client.post(f'/api/v1/snapshots/{snapshot}/verify',headers=headers).json()
        worker.execute(worker.claim())
        assert client.get('/api/v1/jobs/'+verified['id']).json()['result']['status']=='verified'
        destination=tmp_path/'restored.txt'
        preview=client.post(f'/api/v1/snapshots/{snapshot}/restore/preview',headers=headers,
                            json={'path':relative,'destination':str(destination)})
        assert preview.status_code==202,preview.text
        worker.execute(worker.claim())
        result=client.get('/api/v1/jobs/'+preview.json()['id']).json()
        assert result['state']=='succeeded',result['log']
        assert not destination.exists()
        confirm=client.post('/api/v1/restores/'+preview.json()['id']+'/confirm',headers=headers)
        assert confirm.status_code==202,confirm.text
        worker.execute(worker.claim())
        restored=client.get('/api/v1/jobs/'+confirm.json()['id']).json()
        assert restored['state']=='succeeded',restored['log']
        assert destination.read_text()=='restore me'
        assert client.post('/api/v1/restores/'+preview.json()['id']+'/confirm',headers=headers).json()['id']==confirm.json()['id']
        # Previewing a non-overwrite restore on an existing destination fails.
        collision=client.post(f'/api/v1/snapshots/{snapshot}/restore/preview',headers=headers,
                              json={'path':relative,'destination':str(destination)}).json()
        worker.execute(worker.claim())
        assert client.get('/api/v1/jobs/'+collision['id']).json()['state']=='failed'
        assert destination.read_text()=='restore me'
        # Existing content may be overwritten only after an explicit fresh preview.
        destination.write_text('changed before preview')
        overwrite=client.post(f'/api/v1/snapshots/{snapshot}/restore/preview',headers=headers,
                              json={'path':relative,'destination':str(destination),'overwrite':True}).json()
        worker.execute(worker.claim())
        destination.write_text('changed AFTER preview')
        confirmed=client.post('/api/v1/restores/'+overwrite['id']+'/confirm',headers=headers).json()
        worker.execute(worker.claim())
        failed=client.get('/api/v1/jobs/'+confirmed['id']).json()
        assert failed['state']=='failed',failed['log']
        assert 'changé' in failed['error']
        assert destination.read_text()=='changed AFTER preview'
        fresh=client.post(f'/api/v1/snapshots/{snapshot}/restore/preview',headers=headers,
                          json={'path':relative,'destination':str(destination),'overwrite':True}).json()
        worker.execute(worker.claim())
        confirmed=client.post('/api/v1/restores/'+fresh['id']+'/confirm',headers=headers).json()
        worker.execute(worker.claim())
        success=client.get('/api/v1/jobs/'+confirmed['id']).json()
        assert success['state']=='succeeded',success['log']
        assert destination.read_text()=='restore me'
        # Whole directories restore through a unique staging name, too.
        directory=tmp_path/'restored-directory'
        preview_dir=client.post(f'/api/v1/snapshots/{snapshot}/restore/preview',headers=headers,
              json={'path':str(selected.parent.relative_to(root)),'destination':str(directory)}).json()
        worker.execute(worker.claim())
        confirmed=client.post('/api/v1/restores/'+preview_dir['id']+'/confirm',headers=headers).json()
        worker.execute(worker.claim())
        directory_job=client.get('/api/v1/jobs/'+confirmed['id']).json()
        assert directory_job['state']=='succeeded',directory_job['log']
        assert (directory/'a.txt').read_text()=='restore me'
        selected.write_text('corrupted')
        checked=client.post(f'/api/v1/snapshots/{snapshot}/verify',headers=headers).json()
        worker.execute(worker.claim())
        assert client.get('/api/v1/jobs/'+checked['id']).json()['state']=='failed'
    finally:worker.close()


def test_webhook_signature_and_durable_retry(environment):
    settings,client=environment
    settings=replace(settings,webhook_url='https://notify.example/hook',webhook_secret='test-signature')
    store=client.app.state.store
    emit(store,settings,'backup.succeeded','job-1')
    class Response:
        status=204
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return b''
    class Opener:
        def open(self,request,timeout):
            import hmac
            assert request.get_header('X-tardis-signature')=='sha256='+hmac.new(b'test-signature',request.data,hashlib.sha256).hexdigest()
            return Response()
    deliver_pending(store,settings,Opener())
    with store.connect() as db:assert db.execute('SELECT state FROM notifications').fetchone()[0]=='sent'
    emit(store,settings,'backup.failed','job-2')
    class Offline:
        def open(self,*args,**kwargs):raise OSError('offline')
    deliver_pending(store,settings,Offline())
    with store.connect() as db:
        row=db.execute("SELECT state,attempts FROM notifications WHERE event='backup.failed'").fetchone()
        assert tuple(row)==('pending',1)


def test_cache_cleanup_preserves_inflight_and_immutable_data(environment):
    settings,client=environment
    work=settings.backup_dir/'work'/'config';(work/'revision-1').mkdir(parents=True)
    (work/'revision-2').mkdir()
    payload=work/'revision-1'/'a';payload.write_text('kept by a snapshot')
    snapshot=settings.backup_dir/'snapshot-proof';os.link(payload,snapshot)
    assert RsnapshotEngine().cleanup(work,{2})==1
    assert snapshot.read_text()=='kept by a snapshot'
    assert (work/'revision-2').is_dir()


def test_missing_envelope_key_never_generates_replacement(environment):
    settings,client=environment
    created=client.post('/api/v1/keys',headers={'X-Tardis-Request':'1'},json={'name':'Protected','private_key':private_key()})
    assert created.status_code==201
    (settings.secret_dir/'envelope.pem').unlink()
    with pytest.raises(RuntimeError,match='Clé privée de coffre absente'):
        initialize(settings)
    assert not (settings.secret_dir/'envelope.pem').exists()
