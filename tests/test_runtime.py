import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi.testclient import TestClient

from app.config import Settings
from app.engines import EngineRegistry
from app.engines import default_registry
from app.engines.rsnapshot import RsnapshotEngine
from app.main import create_app
from app.maintenance import backup_database, database_copies
from app.queue import enqueue
from app.store import Store
from app.worker import Worker, retained_ids

HEADERS = {'X-Tardis-Request':'1'}


@pytest.fixture
def environment(tmp_path):
    settings = Settings('http://testserver',tmp_path/'data',tmp_path/'secrets',tmp_path/'backups',tmp_path/'database-backups')
    with TestClient(create_app(settings)) as client:
        yield settings, client


@pytest.fixture
def ssh_server(tmp_path, request):
    """Real SSH + rsync server in this disposable test container; no LAN access."""
    if not shutil.which('sshd') or os.getuid() != 0:
        pytest.skip('Integration requires the Docker test target with openssh-server.')
    options = getattr(request, 'param', {})
    private = (rsa.generate_private_key(public_exponent=65537,key_size=2048)
               if options.get('client_type')=='rsa' else ed25519.Ed25519PrivateKey.generate())
    host = ed25519.Ed25519PrivateKey.generate()
    def encode(key):
        return key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.OpenSSH,serialization.NoEncryption())
    host_path = tmp_path/'host_key'
    host_path.write_bytes(encode(host)); host_path.chmod(0o600)
    authorized = tmp_path/'authorized_keys'
    authorized.write_bytes(private.public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH)+b'\n')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',options.get('port',0))); port = sock.getsockname()[1]
    config = tmp_path/'sshd.conf'
    config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {host_path}
PidFile {tmp_path}/sshd.pid
AuthorizedKeysFile {authorized}
StrictModes no
PermitRootLogin yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
LogLevel ERROR
''')
    log = open(tmp_path/'sshd.log','w+')
    process = subprocess.Popen(['/usr/sbin/sshd','-D','-e','-f',str(config)],stdout=log,stderr=log)
    try:
        for _ in range(100):
            if process.poll() is not None:
                log.seek(0); raise RuntimeError(log.read())
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        yield {'host':'127.0.0.1','port':port,'private':encode(private).decode()}
    finally:
        process.terminate(); process.wait(timeout=5); log.close()


def setup_backup(client, remote, folders, **extra):
    server = client.post('/api/v1/servers',headers=HEADERS,json={'name':'Isolé','host':remote['host'],'port':remote['port']}).json()
    key = client.post('/api/v1/keys',headers=HEADERS,json={'name':'Isolée','private_key':remote['private']}).json()
    scan = client.get(f"/api/v1/servers/{server['id']}/host-key")
    assert scan.status_code == 200,scan.text
    public = next(k for k in scan.json() if k['public_key'].startswith('ssh-ed25519'))
    approval = client.post(f"/api/v1/servers/{server['id']}/host-key",headers=HEADERS,json=public)
    assert approval.status_code == 200,approval.text
    payload = {'name':'Test réel','server_id':server['id'],'key_id':key['id'],'login':'root',
               'folders':[str(f) for f in folders],'schedule':'23:59',**extra}
    result = client.post('/api/v1/backups',headers=HEADERS,json=payload)
    assert result.status_code == 201,result.text
    return result.json(), payload


def run_job(client, worker, backup, key):
    queued = client.post(f"/api/v1/backups/{backup['id']}/run",headers=HEADERS,json={'idempotency_key':key})
    assert queued.status_code == 202,queued.text
    job = worker.claim(); assert job is not None
    worker.execute(job)
    return client.get('/api/v1/jobs/'+queued.json()['id']).json()


def test_real_rsnapshot_hardlinks_download_and_failed_transfer(environment, ssh_server, tmp_path):
    settings,client = environment
    source = tmp_path/'folder with spaces'
    source.mkdir(); (source/'stable.txt').write_text('stable'); (source/'changed.txt').write_text('before')
    (source/'omit.tmp').write_text('exclude'); (source/'outside').symlink_to('/etc/passwd')
    backup,payload = setup_backup(client,ssh_server,[source],excludes=['*.tmp'],retention={'daily':2,'weekly':0,'monthly':0})
    worker = Worker(settings); worker.acquire()
    try:
        first = run_job(client,worker,backup,'first')
        assert first['state']=='succeeded',first['log']
        first_tree = settings.backup_dir/'snapshots'/first['id']
        old_stable = next(first_tree.rglob('stable.txt'))
        old_changed = next(first_tree.rglob('changed.txt'))
        assert not list(first_tree.rglob('omit.tmp'))
        (source/'changed.txt').write_text('after longer')
        os.utime(source/'changed.txt',(time.time()+5,time.time()+5))
        second = run_job(client,worker,backup,'second')
        assert second['state']=='succeeded',second['log']
        second_tree = settings.backup_dir/'snapshots'/second['id']
        new_stable = next(second_tree.rglob('stable.txt'))
        assert old_stable.stat().st_ino == new_stable.stat().st_ino
        assert old_changed.read_text() == 'before'
        assert next(second_tree.rglob('changed.txt')).read_text() == 'after longer'
        path = str(old_changed.relative_to(first_tree))
        restored = client.get(f"/api/v1/snapshots/{first['id']}/download",params={'path':path})
        assert restored.content == b'before'
        assert client.get(f"/api/v1/snapshots/{first['id']}/download",params={'path':'../../etc/passwd'}).status_code==400
        link = str(next(first_tree.rglob('outside')).relative_to(first_tree))
        assert client.get(f"/api/v1/snapshots/{first['id']}/download",params={'path':link}).status_code==400
        # Preflight succeeds, then remove the source just before the real rsync.
        class InterruptedSource(RsnapshotEngine):
            def capture(self,context,destination):
                shutil.rmtree(source)
                super().capture(context,destination)
        worker.registry = EngineRegistry(); worker.registry.register(InterruptedSource())
        failed = run_job(client,worker,backup,'failed-transfer')
        assert failed['state']=='failed',failed['log']
        assert 'rsnapshot' in failed['error']
        assert len(client.get('/api/v1/snapshots').json())==2
        assert old_changed.read_text()=='before'
        assert not (settings.backup_dir/'snapshots'/failed['id']).exists()
        # An active download lease prevents pruning its version.
        import fcntl
        lease=open(first_tree/'.lease','rb'); fcntl.flock(lease,fcntl.LOCK_SH)
        try:
            source.mkdir(); (source/'stable.txt').write_text('stable'); (source/'changed.txt').write_text('third')
            worker.registry=default_registry()
            third=run_job(client,worker,backup,'third')
            assert third['state']=='succeeded',third['log']
            assert len(client.get('/api/v1/snapshots').json())==3
        finally: lease.close()
        with worker.store.connect() as db:
            config=json.loads(db.execute('SELECT configuration FROM jobs WHERE id=?',(third['id'],)).fetchone()[0])
        worker.prune(config)
        assert len(client.get('/api/v1/snapshots').json())==2
        assert not first_tree.exists()
    finally:
        worker.close()


def test_sqlite_online_copy_while_writes_and_recovery(environment):
    settings, client = environment
    path = settings.data_dir/'tardis.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE proof (n INTEGER)'); db.execute('INSERT INTO proof VALUES (1)')
    stop = threading.Event()
    def writer():
        with sqlite3.connect(path,timeout=10) as db:
            while not stop.is_set():
                db.execute('INSERT INTO proof VALUES (2)'); db.commit(); stop.wait(.002)
    thread=threading.Thread(target=writer); thread.start()
    try:
        for _ in range(3): backup_database(path,settings.database_backup_dir,keep=2)
    finally:
        stop.set(); thread.join()
    copies=database_copies(settings.database_backup_dir)
    assert len(copies)==2
    for record in copies:
        with sqlite3.connect(settings.database_backup_dir/record['filename']) as restored:
            assert restored.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert restored.execute('SELECT count(*) FROM proof').fetchone()[0]>=1
        download=client.get('/api/v1/database-backups/'+record['id']+'/download')
        assert download.status_code==200
        assert download.content.startswith(b'SQLite format 3')


def test_future_engine_registered_without_changes_to_queue(environment, ssh_server, tmp_path):
    settings, client=environment
    source=tmp_path/'source'; source.mkdir()
    backup,payload=setup_backup(client,ssh_server,[source])
    registry=EngineRegistry()
    class OtherEngine:
        name='another-tool'; label='A future backend'
        def capture(self, context, destination):
            destination.mkdir(); (destination/'proof.txt').write_text('generic publication')
    registry.register(OtherEngine())
    with client.app.state.store.connect() as db:
        db.execute("UPDATE backups SET engine='another-tool' WHERE id=?",(backup['id'],))
    worker=Worker(settings,registry); worker.acquire()
    try:
        job=enqueue(worker.store,backup['id'],registry=registry)
        worker.execute(worker.claim())
        result=client.get('/api/v1/jobs/'+job['id']).json()
        assert result['state']=='succeeded',result['log']
        assert client.get('/api/v1/snapshots').json()[0]['engine']=='another-tool'
    finally: worker.close()


def test_host_approval_idempotence_queue_persistence_scheduler(environment, ssh_server, tmp_path):
    settings,client=environment
    source=tmp_path/'source'; source.mkdir()
    backup,payload=setup_backup(client,ssh_server,[source],schedule='02:00')
    worker=Worker(settings); worker.acquire()
    try:
        moment=datetime(2026,11,16,12,tzinfo=timezone.utc)
        worker.schedule(moment); worker.schedule(moment)
        jobs=client.get('/api/v1/jobs').json()
        assert len(jobs)==1
        assert jobs[0]['trigger']=='schedule'
        assert client.post('/api/v1/backups/'+backup['id']+'/run',headers=HEADERS,json={}).status_code==409
        claimed=worker.claim()
        assert claimed
        worker.close()
        restarted=Worker(settings); restarted.acquire(); restarted.recover()
        try:
            assert client.get('/api/v1/jobs/'+claimed['id']).json()['state']=='failed'
            first=client.post('/api/v1/backups/'+backup['id']+'/test',headers=HEADERS,json={'idempotency_key':'same'})
            second=client.post('/api/v1/backups/'+backup['id']+'/test',headers=HEADERS,json={'idempotency_key':'same'})
            assert first.json()['id']==second.json()['id']
            restarted.execute(restarted.claim())
            assert client.get('/api/v1/jobs/'+first.json()['id']).json()['state']=='succeeded'
        finally: restarted.close()
    finally: worker.close()


def test_wrong_host_key_fails_closed(environment,ssh_server,tmp_path):
    settings,client=environment
    source=tmp_path/'source'; source.mkdir()
    backup,payload=setup_backup(client,ssh_server,[source])
    wrong=ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    with client.app.state.store.connect() as db:
        db.execute('UPDATE trusted_hosts SET public_key=?',(wrong,))
    worker=Worker(settings); worker.acquire()
    try:
        result=run_job(client,worker,backup,'wrong-key')
        assert result['state']=='failed'
        assert client.get('/api/v1/snapshots').json()==[]
    finally:worker.close()


def test_retention_keeps_week_month_and_latest_versions():
    rows=[{'id':str(i),'created_at':f'2026-{m:02d}-{d:02d}T12:00:00+00:00'} for i,(m,d) in enumerate([(9,1),(10,1),(11,1),(11,9),(11,16)])]
    keep=retained_ids(rows,{'daily':1,'weekly':2,'monthly':3},'Europe/Paris')
    assert keep=={'0','1','3','4'}


def test_pre_migration_copy_preserves_v1(environment):
    settings,client=environment
    path=settings.data_dir/'tardis.db'
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE backups DROP COLUMN engine')
        db.execute('ALTER TABLE backups DROP COLUMN timeout_seconds')
        db.execute('ALTER TABLE backups DROP COLUMN engine_options')
        db.execute('PRAGMA user_version=1')
    Store(path,settings.database_backup_dir)
    copy=database_copies(settings.database_backup_dir)[0]
    with sqlite3.connect(settings.database_backup_dir/copy['filename']) as old:
        assert old.execute('PRAGMA user_version').fetchone()[0]==1
    with sqlite3.connect(path) as upgraded:
        assert upgraded.execute('PRAGMA user_version').fetchone()[0]==3


def test_manual_database_job_and_unknown_engine(environment):
    settings,client=environment
    worker=Worker(settings); worker.acquire()
    try:
        queued=client.post('/api/v1/database-backups',headers=HEADERS)
        assert queued.status_code==202
        worker.execute(worker.claim())
        assert client.get('/api/v1/database-backups').json()[0]['integrity']=='ok'
        assert client.get('/api/v1/engines').json()[0]['id']=='rsnapshot'
    finally:worker.close()


def test_automatic_database_copy_runs_while_backup_worker_is_busy(environment):
    settings,client=environment
    worker=Worker(settings); worker.acquire()
    maintenance=threading.Thread(target=worker.maintenance_loop)
    maintenance.start()
    try:
        for _ in range(100):
            if database_copies(settings.database_backup_dir):break
            time.sleep(.02)
        assert database_copies(settings.database_backup_dir)[0]['integrity']=='ok'
    finally:
        worker.stop.set(); maintenance.join(timeout=5); worker.close()


def test_job_cancel_and_single_worker_lock(environment,ssh_server,tmp_path):
    settings,client=environment
    source=tmp_path/'source'; source.mkdir()
    backup,_=setup_backup(client,ssh_server,[source])
    worker=Worker(settings); worker.acquire()
    other=Worker(settings)
    try:
        with pytest.raises(RuntimeError,match='déjà actif'):other.acquire()
        queued=client.post('/api/v1/backups/'+backup['id']+'/run',headers=HEADERS,json={}).json()
        assert client.post('/api/v1/jobs/'+queued['id']+'/cancel',headers=HEADERS).status_code==200
        assert worker.claim() is None
        assert client.get('/api/v1/jobs/'+queued['id']).json()['state']=='cancelled'
    finally:other.close();worker.close()


def test_actual_collection_under_production_unprivileged_uid(ssh_server):
    # Production runs as UID 10001, unlike the disposable SSH server fixture.
    with tempfile.TemporaryDirectory(prefix='tardis-uid-') as directory:
        root=Path(directory); root.chmod(0o755)
        settings=Settings('http://testserver',root/'data',root/'secrets',root/'backups',root/'copies')
        source=root/'source';source.mkdir();(source/'proof.txt').write_text('unprivileged worker')
        with TestClient(create_app(settings)) as client:
            backup,_=setup_backup(client,ssh_server,[source])
            queued=client.post('/api/v1/backups/'+backup['id']+'/run',headers=HEADERS,json={}).json()
            for path in [root,*root.rglob('*')]:
                os.chown(path,10001,10001)
            env={**os.environ,'TARDIS_PUBLIC_URL':'http://testserver','TARDIS_DATA_DIR':str(settings.data_dir),
                 'TARDIS_SECRET_DIR':str(settings.secret_dir),'TARDIS_BACKUP_DIR':str(settings.backup_dir),
                 'TARDIS_DATABASE_BACKUP_DIR':str(settings.database_backup_dir)}
            result=subprocess.run(['runuser','-u','tardis','--','python','-c',
                'from app.worker import Worker; w=Worker(); w.acquire(); w.execute(w.claim()); w.close()'],
                env=env,capture_output=True,timeout=30)
            assert result.returncode==0,result.stderr.decode()
            job=client.get('/api/v1/jobs/'+queued['id']).json()
            assert job['state']=='succeeded',job['log']
            assert next((settings.backup_dir/'snapshots'/job['id']).rglob('proof.txt')).read_text()=='unprivileged worker'


@pytest.mark.parametrize('ssh_server',[{'port':22,'client_type':'rsa'}],indirect=True)
def test_rsa_login_key_with_ed25519_host_on_standard_port(environment,ssh_server,tmp_path):
    settings,client=environment
    source=tmp_path/'source';source.mkdir();(source/'proof.txt').write_text('RSA user key, ED25519 host key')
    backup,_=setup_backup(client,ssh_server,[source])
    imported=client.get('/api/v1/keys').json()[0]
    assert imported['key_type']=='ssh-rsa'
    assert client.get('/api/v1/servers').json()[0]['host_fingerprint']
    worker=Worker(settings);worker.acquire()
    try:
        result=run_job(client,worker,backup,'rsa-standard-port')
        assert result['state']=='succeeded',result['log']
        assert next((settings.backup_dir/'snapshots'/result['id']).rglob('proof.txt')).read_text()=='RSA user key, ED25519 host key'
    finally:worker.close()
