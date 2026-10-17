import pytest
from integrations.email import EmailService, DevelopmentInbox, TransactionalTransport
from integrations.fakes import FakeEmailTransport, fake_models


def test_durable_idempotency(tmp_path):
    transport = FakeEmailTransport()
    ledger = tmp_path / 'mail.sqlite'
    service = EmailService(transport, ledger, factory=fake_models)
    first = service.send_invitation('test@example.test', 'https://example.test/accept?token=secret', 'key')
    service.db.close()
    service = EmailService(transport, ledger, factory=fake_models)
    second = service.send_invitation('test@example.test', 'https://example.test/accept?token=secret', 'key')
    assert first.status == second.status == 'development_inbox'
    assert transport.calls == 1
    assert b'secret' not in ledger.read_bytes()
    with pytest.raises(ValueError, match='idempotency_conflict'):
        service.send_invitation('other@example.test', 'different', 'key')


def test_failure_is_not_delivery(tmp_path):
    service = EmailService(FakeEmailTransport(fail=True), tmp_path / 'mail.sqlite', factory=fake_models)
    result = service.send_invitation('test@example.test', 'secret', 'key')
    assert result.status == 'failed' and result.message_id is None
    assert 'sensitive' not in result.error
    service.send_invitation('test@example.test', 'secret', 'key')
    assert service.transport.calls == 1


def test_dev_inbox_guard_and_permissions(tmp_path):
    with pytest.raises(ValueError): DevelopmentInbox(tmp_path, environment='production')
    transport = DevelopmentInbox(tmp_path / 'inbox', environment='development')
    service = EmailService(transport, tmp_path / 'mail.sqlite', factory=fake_models)
    result = service.send_invitation('test@example.test', 'secret', 'key')
    assert result.status == 'development_inbox'
    path = next((tmp_path / 'inbox').glob('*.json'))
    assert path.stat().st_mode & 0o777 == 0o600


def test_transactional_receipt(tmp_path):
    transport = TransactionalTransport(lambda **_: 'provider-real-receipt')
    service = EmailService(transport, tmp_path / 'mail.sqlite', factory=fake_models)
    assert service.send_invitation('test@example.test', 'secret', 'key').status == 'sent'
