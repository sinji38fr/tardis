import fcntl
import hashlib
import json
import os
import shlex
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath


@contextmanager
def snapshot_source(store,settings,identifier,path=''):
    with store.connect() as db:
        row=db.execute("SELECT * FROM snapshots WHERE id=? AND state='ready'",(identifier,)).fetchone()
    if row is None:raise ValueError('Version indisponible.')
    root=settings.backup_dir/'snapshots'/identifier
    with open(root/'.lease','rb') as lease:
        fcntl.flock(lease,fcntl.LOCK_SH)
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM snapshots WHERE id=? AND state='ready'",(identifier,)).fetchone():
                raise ValueError('Version expirée.')
        relative=PurePosixPath(path or '.')
        if relative.is_absolute() or '..' in relative.parts or any(p in ('.lease','.tardis.json') for p in relative.parts) or '\\' in path or any(ord(c)<32 for c in path):
            raise ValueError('Chemin source refusé.')
        selected=root
        for part in relative.parts:
            selected/=part
            if selected.is_symlink():raise ValueError('Les liens symboliques ne sont pas suivis.')
        if not selected.exists():raise ValueError('Fichier source introuvable.')
        yield selected,dict(row)


def source_summary(selected,checkpoint=lambda:None):
    total,size,preview=0,0,[]
    pending=[selected]
    while pending:
        checkpoint(); path=pending.pop()
        if path.is_symlink():continue
        if path.is_dir():pending.extend(sorted(path.iterdir(),reverse=True))
        elif path.is_file():
            total+=1;size+=path.stat().st_size
            if len(preview)<2000:preview.append(str(path.relative_to(selected)) if selected.is_dir() else path.name)
    return {'files':total,'bytes':size,'preview':preview,'truncated':total>len(preview)}


def restore_commands(configuration,selected,options,destination=None):
    operation=configuration['operation']
    destination=destination or operation['destination']
    target=configuration['server']['host']
    if ':' in target:target=f'[{target}]'
    source=str(selected)+('/' if selected.is_dir() else '')
    remote=f"{configuration['login']}@{target}:{destination}"+('/' if selected.is_dir() else '')
    return ['/usr/bin/rsync','-a','--numeric-ids','--checksum','--protect-args','--safe-links',
            '-e',shlex.join(['/usr/bin/ssh',*options]),'--',source,remote]


def restore_operation(worker,job,configuration,options,run):
    operation=configuration['operation']
    with snapshot_source(worker.store,worker.settings,operation['snapshot_id'],operation['path']) as (selected,row):
        if not selected.is_dir() and not selected.is_file():raise ValueError('Source de restauration non prise en charge.')
        destination=operation['destination']; parent=str(PurePosixPath(destination).parent)
        quote=shlex.quote; target=f"{configuration['login']}@{configuration['server']['host']}"
        ssh=['/usr/bin/ssh',*options,target]
        checks=f'test -d {quote(parent)} && test ! -L {quote(destination)}'
        if not operation['overwrite']:checks+=f' && test ! -e {quote(destination)}'
        run(ssh+[checks])
        # rsync's itemized flags describe a category of change, not the previous
        # destination bytes. Hash the destination itself before accepting a plan.
        state_command=(f'if test -d {quote(destination)}; then '
                       f'find {quote(destination)} -type f -exec sha256sum --zero -- {{}} +; '
                       f'elif test -f {quote(destination)}; then sha256sum --zero -- {quote(destination)}; '
                       f'elif test -e {quote(destination)}; then exit 1; else printf absent; fi')
        run(ssh+[state_command],log_output=False)
        destination_digest=run.last_digest
        dryrun=restore_commands(configuration,selected,options)
        dryrun[1:1]=['--dry-run','--itemize-changes']
        changes=run(dryrun)
        digest=run.last_digest
        if job['kind']=='restore_preview':
            return {**source_summary(selected,run.checkpoint),'destination':destination,
                    'server':configuration['server']['host'],'login':configuration['login'],
                    'overwrite':operation['overwrite'],'changes':changes,'digest':digest,
                    'destination_digest':destination_digest}
        if operation.get('digest')!=digest or operation.get('destination_digest')!=destination_digest:
            raise RuntimeError('La destination a changé depuis l’aperçu ; créer un nouvel aperçu.')
        if operation['overwrite']:
            run(restore_commands(configuration,selected,options))
        else:
            staging=str(PurePosixPath(parent)/('.tardis-restore-'+job['id']))
            # Restore to a unique remote staging name; publish with no-clobber so
            # another process creating the destination cannot be overwritten.
            if selected.is_dir():run(ssh+[f'mkdir -- {quote(staging)}'])
            run(restore_commands(configuration,selected,options,staging))
            run(ssh+[f'mv -T -n -- {quote(staging)} {quote(destination)} && test ! -e {quote(staging)} && test ! -L {quote(staging)}'])
        return {'restored':True,'destination':destination,'overwrite':operation['overwrite']}


def verify_operation(worker,configuration,run):
    operation=configuration['operation'];identifier=operation['snapshot_id']
    with snapshot_source(worker.store,worker.settings,identifier) as (root,row):
        reference=worker.settings.backup_dir/'integrity'/f'{identifier}.json'
        reference.parent.mkdir(parents=True,exist_ok=True)
        fingerprints={}
        for current,dirs,files in os.walk(root,followlinks=False):
            for name in files:
                path=root.__class__(current)/name
                relative=str(path.relative_to(root))
                if relative in ('.lease','.tardis.json'):continue
                run.checkpoint()
                if not stat.S_ISREG(path.lstat().st_mode):continue
                digest=hashlib.sha256()
                with open(path,'rb') as handle:
                    while block:=handle.read(1024*1024):
                        run.checkpoint();digest.update(block)
                fingerprints[relative]=digest.hexdigest()
        if not reference.exists():
            pending=reference.with_suffix('.part')
            pending.write_text(json.dumps(fingerprints,sort_keys=True),encoding='utf-8')
            os.replace(pending,reference)
            return {'status':'reference_created','files':len(fingerprints)}
        expected=json.loads(reference.read_text())
        changed=[p for p in sorted(set(expected)|set(fingerprints)) if expected.get(p)!=fingerprints.get(p)]
        if changed:
            worker.append_log(configuration['operation']['job_id'],'Différences SHA256 : '+', '.join(changed[:100]))
            raise RuntimeError(f'Contrôle SHA256 échoué : {len(changed)} fichier(s) modifié(s), absent(s) ou ajouté(s).')
        return {'status':'verified','files':len(fingerprints)}
