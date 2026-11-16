import hashlib
import hmac
import json
import urllib.request
from datetime import datetime, timedelta, timezone

from .store import now, uid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def emit(store, settings, event, resource_id, details=None):
    if not settings.webhook_url:
        return
    payload = {'id':uid(),'event':event,'resource_id':resource_id,'created_at':now(),
               'details':details or {}}
    with store.connect() as db:
        db.execute('INSERT INTO notifications (id,event,payload,available_at,created_at) VALUES (?,?,?,?,?)',
                   (payload['id'],event,json.dumps(payload,ensure_ascii=False),now(),now()))


def deliver_pending(store,settings,opener=None):
    if not settings.webhook_url:
        return
    opener = opener or urllib.request.build_opener(NoRedirect())
    with store.connect() as db:
        rows = list(db.execute("SELECT * FROM notifications WHERE state='pending' AND available_at<=? ORDER BY created_at LIMIT 10",(now(),)))
    for row in rows:
        data = row['payload'].encode()
        headers = {'Content-Type':'application/json','User-Agent':'TARDIS/1.0','X-Tardis-Event':row['event'],
                   'X-Tardis-Delivery':row['id']}
        if settings.webhook_secret:
            headers['X-Tardis-Signature'] = 'sha256=' + hmac.new(settings.webhook_secret.encode(),data,hashlib.sha256).hexdigest()
        request=urllib.request.Request(settings.webhook_url,data=data,headers=headers,method='POST')
        try:
            with opener.open(request,timeout=10) as response:
                if response.status < 200 or response.status >= 300:
                    raise ValueError('Réponse HTTP non réussie.')
                response.read(4096)
            state,error='sent',None
        except Exception:
            state='failed' if row['attempts']+1>=5 else 'pending'
            error='Livraison du webhook échouée.'
        next_at=(datetime.now(timezone.utc)+timedelta(seconds=min(3600,30*2**row['attempts']))).isoformat()
        with store.connect() as db:
            db.execute('UPDATE notifications SET state=?,attempts=attempts+1,available_at=?,error=? WHERE id=?',
                       (state,next_at,error,row['id']))
