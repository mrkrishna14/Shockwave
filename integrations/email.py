"""Transport-only invitation delivery. Tokens are never logged or stored in the ledger."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from shared.schemas import DeliveryResult

import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4
from integrations.contracts import canonical


class DevelopmentInbox:
    def __init__(self, root: Path, *, environment: str):
        if environment not in ('development', 'test'):
            raise ValueError('development_inbox_forbidden')
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def send(self, recipient, url, idempotency_key):
        message_id = hashlib.sha256(idempotency_key.encode()).hexdigest()
        path = self.root / (message_id + '.json')
        try:
            with path.open('x', encoding='utf-8') as stream:
                path.chmod(0o600)
                json.dump({'recipient': recipient, 'acceptance_url': url}, stream)
        except FileExistsError:
            pass
        return 'development_inbox', message_id


class TransactionalTransport:
    """Inject a configured provider client accepting a provider-side idempotency key.

    Callable returns an actual accepted message ID or raises; no success inference.
    SMTP alone cannot provide the required retry idempotency guarantee.
    """
    def __init__(self, send):
        self._send = send

    def send(self, recipient, url, idempotency_key):
        message_id = self._send(recipient=recipient, acceptance_url=url,
                                idempotency_key=idempotency_key)
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('missing_delivery_receipt')
        return 'sent', message_id


class EmailService:
    def __init__(self, transport, ledger: Path, *, factory=canonical):
        self.transport, self.factory, self.lock = transport, factory, Lock()
        self.db = sqlite3.connect(ledger, check_same_thread=False)
        ledger.chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, message_id TEXT, error TEXT)')
        self.db.commit()

    def send_invitation(self, recipient_email: str, acceptance_url: str, idempotency_key: str) -> DeliveryResult:
        if not idempotency_key or any(c in recipient_email for c in '\r\n'):
            raise ValueError('invalid_delivery_input')
        key = hashlib.sha256(idempotency_key.encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([recipient_email, acceptance_url]).encode()).hexdigest()
        with self.lock:
            # Claim before sending; ambiguous/crashed sends cannot be silently retried.
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT fingerprint,status,message_id,error FROM deliveries WHERE key=?', (key,)).fetchone()
            if row:
                self.db.commit()
                if row[0] != fingerprint:
                    raise ValueError('idempotency_conflict')
                return self.factory('DeliveryResult', status=row[1], message_id=row[2], error=row[3])
            self.db.execute('INSERT INTO deliveries VALUES (?,?,?,?,?)',
                            (key, fingerprint, 'failed', None, 'delivery_pending_or_unknown'))
            self.db.commit()
            try:
                status, message_id = self.transport.send(recipient_email, acceptance_url, idempotency_key)
                if status not in ('sent', 'development_inbox') or not message_id:
                    raise ValueError('invalid_transport_receipt')
                error = None
            except Exception:
                status, message_id, error = 'failed', None, 'transport_failed_or_unknown'
            self.db.execute('UPDATE deliveries SET status=?,message_id=?,error=? WHERE key=?',
                            (status, message_id, error, key))
            self.db.commit()
            return self.factory('DeliveryResult', status=status, message_id=message_id, error=error)
