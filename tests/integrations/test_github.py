import hashlib
import hmac
import io
import tarfile
import pytest
from integrations.github import GitHubAdapter, GitHubError, Response, extract_snapshot, verify_webhook
from integrations.fakes import FakeGitHubTransport, fake_models, ContractDouble as D


def archive(name='root/consumer.py', kind=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        item = tarfile.TarInfo(name)
        if kind:
            item.type = kind
            item.linkname = '/tmp/escape'
            tar.addfile(item)
        else:
            item.size = 3
            tar.addfile(item, io.BytesIO(b'x=1'))
    return buffer.getvalue()


def adapter(tmp_path, routes):
    return GitHubAdapter(FakeGitHubTransport(routes), lambda: 'private-token', tmp_path / 'cache',
                         lambda _: True, factory=fake_models)


def test_pagination(tmp_path):
    client = adapter(tmp_path, {
        ('GET', '/installation/repositories?per_page=100&page=1'): Response(200, {'repositories': [{'id': i} for i in range(100)]}, {}),
        ('GET', '/installation/repositories?per_page=100&page=2'): Response(200, {'repositories': [{'id': 100}]}, {}),
    })
    assert len(client.pages('/installation/repositories', 'secret', 'repositories')) == 101
    assert 'secret' not in repr(client.transport.calls)


@pytest.mark.parametrize('status,headers,message', [
    (401, {}, 'revoked'), (403, {}, 'inaccessible'), (404, {}, 'inaccessible'),
    (429, {}, 'rate_limited'), (403, {'x-ratelimit-remaining': '0'}, 'rate_limited')])
def test_api_error_categories(tmp_path, status, headers, message):
    client = adapter(tmp_path, {('GET', '/x'): Response(status, {}, headers)})
    with pytest.raises(GitHubError, match=message): client.request('GET', '/x', 'secret')


@pytest.mark.parametrize('name,kind', [('root/../../escape', None), ('/tmp/escape', None),
    ('root/link', tarfile.SYMTYPE), ('root/hard', tarfile.LNKTYPE), ('root/device', tarfile.CHRTYPE),
    ('root/.git/config', None), ('root/back\\slash', None)])
def test_snapshot_escape_rejected(tmp_path, name, kind):
    with pytest.raises(GitHubError): extract_snapshot(archive(name, kind), tmp_path)
    assert not list(tmp_path.iterdir())


def test_webhook_signature():
    body, secret = b'{"action":"deleted"}', b'secret'
    sig = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert verify_webhook(body, sig, secret)
    for bad in ['', 'sha1=abc', sig[:-1], 'sha256=' + 'f' * 64]:
        assert not verify_webhook(body, bad, secret)
    assert not verify_webhook(body + b' ', sig, secret)


def test_callback_requires_state_and_user_access(tmp_path):
    client = adapter(tmp_path, {('GET', '/user/installations?per_page=100&page=1'):
        Response(200, {'installations': [{'id': 1}]}, {})})
    with pytest.raises(GitHubError, match='invalid_oauth_state'):
        client.verify_installation_binding(1, 'secret', state='x', consume_state=lambda _: False)
    assert not client.transport.calls
    with pytest.raises(GitHubError, match='not_accessible'):
        client.verify_installation_binding(2, 'secret', state='x', consume_state=lambda _: True)


def test_pinned_fetch_cache_rechecks_and_revocation(tmp_path):
    sha = 'a' * 40
    routes = {
        ('POST', '/app/installations/1/access_tokens'): Response(201, {'token': 'private', 'permissions': {'contents': 'read'}}, {}),
        ('GET', '/installation/repositories?per_page=100&page=1'): Response(200, {'repositories': [{'id': 2, 'full_name': 'team/demo'}]}, {}),
        ('GET', f'/repos/team/demo/commits/{sha}'): Response(200, {'sha': sha}, {}),
        ('GET', f'/repos/team/demo/tarball/{sha}'): Response(200, archive(), {}),
    }
    client = adapter(tmp_path, routes)
    connection = D(id='conn', installation_id=1, github_repository_id=2, status='connected', scan_consent_at='now')
    first = client.fetch_repository(connection, sha)
    second = client.fetch_repository(connection, sha)
    assert first.content_hash == second.content_hash
    assert sum('tarball' in c[1] for c in client.transport.calls) == 1
    routes[('GET', '/installation/repositories?per_page=100&page=1')] = Response(200, {'repositories': []}, {})
    with pytest.raises(GitHubError, match='repository_removed'): client.fetch_repository(connection, sha)
    connection.status = 'revoked'
    with pytest.raises(GitHubError, match='revoked'): client.fetch_repository(connection, sha)
    client.purge_repository(2)
    assert not list(client.cache.iterdir())


def test_suspended_installation_is_rejected(tmp_path):
    client = adapter(tmp_path, {
        ('GET', '/user/installations?per_page=100&page=1'): Response(200, {'installations': [{'id': 1}]}, {}),
        ('GET', '/app/installations/1'): Response(200, {'id': 1, 'suspended_at': 'now'}, {}),
    })
    with pytest.raises(GitHubError, match='installation_revoked'):
        client.verify_installation_binding(1, 'secret', state='x', consume_state=lambda _: True)


def test_archive_size_bound(tmp_path):
    with pytest.raises(GitHubError, match='too_large'):
        extract_snapshot(archive(), tmp_path, max_bytes=1)
