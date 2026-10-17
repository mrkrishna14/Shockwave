"""Stateful GitHub API double; no outbound writes occur in these tests."""
import importlib.util
from pathlib import Path
from urllib.parse import unquote
import pytest
from integrations.github import GitHubAdapter, GitHubError, Response
from integrations.fakes import ContractDouble as D, fake_models
from migration.publication import PublicationService

spec = importlib.util.spec_from_file_location('publication_support', Path(__file__).parents[1] / 'verification/support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class PublicationHTTPDouble:
    def __init__(self):
        self.head = 'a' * 40
        self.branch = None
        self.pr = None
        self.pr_creations = 0
        self.revoked = False
        self.permission = 'write'
        self.calls = []

    def request(self, method, path, *, token, body=None):
        self.calls.append((method, path))
        if self.revoked: return Response(401, {}, {})
        if path.endswith('/access_tokens'):
            return Response(201, {'token': 'hidden', 'permissions': {'contents': self.permission, 'pull_requests': self.permission}}, {})
        if path.startswith('/installation/repositories'):
            return Response(200, {'repositories': [{'id': 2, 'full_name': 'team/demo'}]}, {})
        path = unquote(path.removeprefix('/repos/team/demo'))
        if path == '/git/ref/heads/main': data = {'object': {'sha': self.head}}
        elif path.startswith('/git/ref/heads/shockwave/'):
            if not self.branch: return Response(404, {}, {})
            data = {'object': {'sha': 'newcommit'}}
        elif path == '/git/commits/' + 'a' * 40: data = {'tree': {'sha': 'basetree'}}
        elif path == '/git/commits/newcommit': data = {'tree': {'sha': 'newtree'}, 'parents': [{'sha': 'a' * 40}]}
        elif path == '/git/trees': data = {'sha': 'newtree'}
        elif path == '/git/commits': data = {'sha': 'newcommit'}
        elif path == '/git/refs': self.branch = body['ref']; data = {'ref': self.branch}
        elif path.startswith('/pulls?'): data = [self.pr] if self.pr else []
        elif path == '/pulls':
            self.pr_creations += 1
            self.pr = {'html_url': 'https://github.com/team/demo/pull/1'}
            data = self.pr
        else: raise AssertionError((method, path))
        return Response(200, data, {})


@pytest.fixture
def prepared(tmp_path):
    s = support.setup(tmp_path)
    verification = s.runner.verify_regression(s.plan, s.root)
    migration = s.migrations.generate_migration(support.request(verification), s.risk, verification, s.root)
    assert migration.status == 'patch_validated'
    transport = PublicationHTTPDouble()
    github = GitHubAdapter(transport, lambda: 'secret', tmp_path / 'cache', lambda _: True, factory=fake_models)
    connection = D(id='connection', status='connected', scan_consent_at='now', allow_pr_write=True,
        installation_id=1, github_repository_id=2, full_name='team/demo', default_branch='main')
    # Real backend must validate membership + org + snapshot binding + action + freshness.
    publisher = PublicationService(github, s.migrations, lambda *_: True, live_enabled=True)
    return D(publisher=publisher, transport=transport, connection=connection, migration=migration)


def test_publication_retry_is_idempotent(prepared):
    s = prepared
    first = s.publisher.publish_migration(s.migration, s.connection, D())
    # Simulate replay of the original request, including after adapter reconstruction.
    second = s.publisher.publish_migration(s.migration, s.connection, D())
    assert first.pr_url == second.pr_url
    assert first.status == 'pr_opened'
    assert s.transport.pr_creations == 1
    assert sum(p == '/repos/team/demo/git/refs' for _, p in s.transport.calls) == 1


@pytest.mark.parametrize('case,error', [('stale', 'stale_base'), ('write', 'write_grant'),
    ('revoked', 'revoked'), ('auth', 'not_authorized'), ('permissions', 'missing_installation'),
    ('disabled', 'not_enabled'), ('tamper', 'validated_patch')])
def test_publication_guards(prepared, case, error):
    s = prepared
    if case == 'stale': s.transport.head = 'b' * 40
    if case == 'write': s.connection.allow_pr_write = False
    if case == 'revoked': s.transport.revoked = True
    if case == 'auth': s.publisher.authorize = lambda *_: False
    if case == 'permissions': s.transport.permission = 'read'
    if case == 'disabled': s.publisher.live_enabled = False
    if case == 'tamper': s.migration.patch_artifact_id = 'forged'
    with pytest.raises(GitHubError, match=error):
        s.publisher.publish_migration(s.migration, s.connection, D())
    assert s.transport.pr_creations == 0
