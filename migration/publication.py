"""Publication behind current backend authorization; never merges or deploys."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from shared.schemas import MigrationResult, RepositoryConnection, PublicationAuthorization

import hashlib
import re
from urllib.parse import quote
from integrations.github import GitHubError
from integrations.storage import safe_path
from verification.runner.service import passed


class PublicationService:
    def __init__(self, github, migrations, authorize, *, live_enabled=False):
        self.github, self.migrations, self.authorize = github, migrations, authorize
        self.live_enabled = live_enabled

    def publish_migration(self, migration: MigrationResult, connection: RepositoryConnection, authorization: PublicationAuthorization) -> MigrationResult:
        receipt = self.migrations.validated.get(migration.id)
        if (not receipt or migration.status not in ('patch_validated', 'pr_opened') or
            not passed(migration.validation) or migration.patch_artifact_id != receipt.patch_artifact_id or
            migration.repository_snapshot_id != receipt.snapshot_id or
            migration.target_contract_snapshot_id != receipt.target_contract_snapshot_id or
            tuple(migration.validation.artifact_ids) != receipt.validation_artifact_ids):
            raise GitHubError('validated_patch_required')
        if not self.live_enabled:
            raise GitHubError('live_publication_not_enabled')
        if not connection.allow_pr_write:
            raise GitHubError('write_grant_required')
        # Backend callback rechecks membership, consumer action, snapshot->connection, consent,
        # auth expected commit + org/repo and timestamp. Never construct auth here.
        def check():
            self.github.check_connection(connection)
            if not self.authorize(authorization, connection, receipt):
                raise GitHubError('publication_not_authorized')
        check()
        token = self.github.installation_token(connection.installation_id, connection.github_repository_id, write=True)
        repos = self.github.pages('/installation/repositories', token, 'repositories')
        repo = next((r for r in repos if str(r['id']) == str(connection.github_repository_id)), None)
        if repo is None or repo['full_name'] != connection.full_name:
            raise GitHubError('repository_removed_or_changed')
        name = repo['full_name']
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', name):
            raise GitHubError('invalid_repository_identity')
        base = connection.default_branch
        prefix = f'/repos/{name}'
        def api(method, path, body=None):
            check()  # Recheck revocation before every read/write.
            return self.github.request(method, prefix + path, token, body)
        head = api('GET', '/git/ref/heads/' + quote(base, safe=''))
        if head['object']['sha'] != receipt.commit_sha:
            raise GitHubError('stale_base_commit')
        identity = hashlib.sha256((str(connection.github_repository_id) + receipt.commit_sha +
            receipt.target_contract_snapshot_id + self.migrations.artifacts.get(receipt.patch_artifact_id).decode()).encode()).hexdigest()[:24]
        branch = 'shockwave/migrate-' + identity
        commit = api('GET', '/git/commits/' + receipt.commit_sha)
        entries = []
        for path, content in receipt.edits.items():
            # Paths already validated during generation; keep publication independently strict.
            from pathlib import Path
            safe_path(Path('/tmp/shockwave-path-validation'), path)
            entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'content': content})
        tree = api('POST', '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': entries})
        # Stable ref + GitHub lookup provide cross-process retry recovery.
        try:
            existing = api('GET', '/git/ref/heads/' + quote(branch, safe=''))
        except GitHubError as exc:
            if str(exc) != 'installation_or_repository_inaccessible':
                raise
            existing = None
        if existing:
            branch_commit = api('GET', '/git/commits/' + existing['object']['sha'])
            if branch_commit['tree']['sha'] != tree['sha'] or [p['sha'] for p in branch_commit['parents']] != [receipt.commit_sha]:
                raise GitHubError('publication_branch_conflict')
        else:
            new_commit = api('POST', '/git/commits', {'message': 'Migrate to proposed API contract',
                'tree': tree['sha'], 'parents': [receipt.commit_sha]})
            if api('GET', '/git/ref/heads/' + quote(base, safe=''))['object']['sha'] != receipt.commit_sha:
                raise GitHubError('stale_base_commit')
            api('POST', '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': new_commit['sha']})
        pulls = api('GET', '/pulls?state=all&head=' + quote(name.split('/')[0] + ':' + branch, safe='') + '&base=' + quote(base, safe=''))
        if pulls:
            pr = pulls[0]
        else:
            pr = api('POST', '/pulls', {'title': 'Migrate amount display to proposed API contract', 'head': branch,
                'base': base, 'body': 'Validated against the proposed contract in the registered test scope. '
                'Requires coordinated deployment; old contract compatibility is not claimed. No automatic merge or deployment.'})
        url = pr.get('html_url', '')
        if not re.fullmatch(r'https://github\.com/' + re.escape(name) + r'/pull/\d+', url):
            raise GitHubError('invalid_publication_receipt')
        # Pydantic model_copy is the canonical integration API.
        return migration.model_copy(update={'status': 'pr_opened', 'pr_url': url, 'branch': branch})
