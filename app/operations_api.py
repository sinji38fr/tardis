import json
from datetime import datetime,timedelta,timezone

from fastapi import HTTPException

from .models import RestoreInput
from .operations import snapshot_source
from .queue import enqueue,public_job


def install_operations(api,settings):
    def store():return api.state.store

    @api.post('/api/v1/snapshots/{identifier}/restore/preview',status_code=202)
    def preview(identifier:str,data:RestoreInput):
        try:
            with snapshot_source(store(),settings,identifier,data.path) as (selected,row):
                return public_job(enqueue(store(),row['backup_id'],'restore_preview',operation={
                    'snapshot_id':identifier,**data.model_dump()}))
        except (ValueError,FileNotFoundError) as error:raise HTTPException(409,str(error)) from None

    @api.post('/api/v1/restores/{preview_id}/confirm',status_code=202)
    def confirm(preview_id:str):
        with store().connect() as db:
            row=db.execute("SELECT * FROM jobs WHERE id=? AND kind='restore_preview' AND state='succeeded'",(preview_id,)).fetchone()
            if not row:raise HTTPException(409,'Aperçu réussi requis.')
            claimed=db.execute('SELECT job_id FROM restore_claims WHERE preview_id=?',(preview_id,)).fetchone()
            if claimed:
                return public_job(db.execute('SELECT * FROM jobs WHERE id=?',(claimed[0],)).fetchone())
        if datetime.now(timezone.utc)-datetime.fromisoformat(row['finished_at'])>timedelta(minutes=15):
            raise HTTPException(409,'Aperçu expiré ; créer un nouvel aperçu.')
        configuration=json.loads(row['configuration']);operation=configuration['operation']
        operation['digest']=json.loads(row['result'])['digest']
        operation['destination_digest']=json.loads(row['result'])['destination_digest']
        try:
            with snapshot_source(store(),settings,operation['snapshot_id'],operation['path']):
                job=enqueue(store(),row['backup_id'],'restore',operation=operation,
                            frozen=configuration,idempotency_key='restore:'+preview_id)
            with store().connect() as db:
                db.execute('INSERT OR IGNORE INTO restore_claims VALUES (?,?)',(preview_id,job['id']))
            return public_job(job)
        except (ValueError,FileNotFoundError) as error:raise HTTPException(409,str(error)) from None

    @api.post('/api/v1/snapshots/{identifier}/verify',status_code=202)
    def verify(identifier:str):
        try:
            with snapshot_source(store(),settings,identifier) as (selected,row):
                return public_job(enqueue(store(),row['backup_id'],'verify',operation={'snapshot_id':identifier}))
        except (ValueError,FileNotFoundError) as error:raise HTTPException(409,str(error)) from None

    @api.get('/api/v1/notifications')
    def notifications():
        with store().connect() as db:
            return {'configured':bool(settings.webhook_url),'deliveries':[dict(r) for r in db.execute(
                'SELECT id,event,state,attempts,created_at,error FROM notifications ORDER BY created_at DESC LIMIT 100')]}
