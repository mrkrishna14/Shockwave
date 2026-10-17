"""Durable invitation operations. Ambiguous delivery is never retried blindly."""
import json
from datetime import datetime, timedelta, timezone

from backend.mailer import invitation_message
from backend.store import now, uid
from shared.schemas import DeliveryResult


class Outbox:
    def __init__(self,store,adapter_factory,demo,mailer=None):
        self.store=store
        self.adapter_factory=adapter_factory
        self.demo=demo
        self.mailer=mailer

    @property
    def available(self): return bool(self.mailer) or self.demo

    def queue_invitation(self,db,invitation,notice,operation_key):
        # The notice names the provider and API only; no acceptance token is ever sent.
        record={'invitation_id':invitation['id'],'recipient_email':invitation['recipient_email'],'notice':notice}
        id=uid()
        provider_org=db.execute('SELECT org_id FROM records WHERE id=?',(invitation['id'],)).fetchone()['org_id']
        db.execute('''INSERT INTO outbox(id,kind,owner_org_id,operation_key,payload,status,updated_at)
            VALUES(?,?,?,?,?,?,?)''',(id,'invitation',provider_org,operation_key,
            json.dumps(record),'pending' if self.available else 'unavailable',now()))
        return id

    def pending(self):
        with self.store.read() as db:
            return [row['id'] for row in db.execute("SELECT id FROM outbox WHERE status='pending' ORDER BY rowid LIMIT 20")]

    def reconcile_abandoned(self, timeout_seconds=300):
        """A crashed dispatcher may have delivered; require review after its timeout."""
        cutoff=(datetime.now(timezone.utc)-timedelta(seconds=timeout_seconds)).isoformat()
        with self.store.tx() as db:
            db.execute("""UPDATE outbox SET status='needs_reconciliation',updated_at=?
                WHERE status='dispatching' AND updated_at<?""",(now(),cutoff))

    def dispatch(self,id):
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(id,)).fetchone()
            if not row or row['status']!='pending': return False
            data=json.loads(row['payload'])
            invitation=self.store.get(db,data['invitation_id'],'invitation')
            if not invitation or invitation['status']!='pending':
                db.execute("UPDATE outbox SET status='cancelled',updated_at=? WHERE id=?",(now(),id))
                return False
            db.execute("UPDATE outbox SET status='dispatching',attempts=attempts+1,updated_at=? WHERE id=?",(now(),id))
            operation_key=row['operation_key']
        try:
            if self.mailer: transport=self.mailer
            elif self.demo: transport=self.adapter_factory()
            else: raise RuntimeError('Email adapter unavailable')
            result=DeliveryResult.model_validate(transport.send_invitation(
                data['recipient_email'],data['notice'],operation_key))
        except Exception:
            # The remote side may have sent the message before an exception. Manual
            # reconciliation is required; a second notice is not sent automatically.
            with self.store.tx() as db:
                db.execute("UPDATE outbox SET status='needs_reconciliation',updated_at=? WHERE id=?",(now(),id))
            return False
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(id,)).fetchone()
            if not row or row['status']!='dispatching': return False
            db.execute('UPDATE outbox SET status=?,result=?,updated_at=? WHERE id=?',
                       ('delivered' if result.status!='failed' else 'failed',json.dumps(result.model_dump(mode='json')),now(),id))
            invitation=self.store.get(db,data['invitation_id'],'invitation')
            if invitation and invitation['status']=='pending':
                invitation['delivery_status']=result.status
                self.store.update(db,invitation)
                if result.status=='development_inbox':
                    subject,body=invitation_message(data['notice'])
                    self.store.put(db,'mail',row['owner_org_id'],{'id':uid(),
                        'recipient_email':data['recipient_email'],'subject':subject,'body':body,
                        'invitation_id':invitation['id'],'created_at':now()},invitation['id'])
            return True
