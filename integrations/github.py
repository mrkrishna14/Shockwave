"""GitHub App transport with pinned snapshots and explicit error categories.

HTTP client is injected. It must return Response, bound bytes/time, and never log auth.
No account membership decisions live here; backend supplies current consent checks.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from shared.schemas import RepositoryConnection, RepositorySnapshot

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import io
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from uuid import uuid4
from integrations.contracts import canonical
from integrations.storage import safe_path, tree_hash


class GitHubError(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    data: object
    headers: dict


def verify_webhook(body: bytes, signature: str, secret: bytes) -> bool:
    if not secret or not re.fullmatch(r'sha256=[0-9a-f]{64}', signature or ''):
        return False
    expected = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def extract_snapshot(archive: bytes, destination: Path, max_bytes=32 * 1024 * 1024, max_files=10000):
    """Reject all links/devices, duplicate paths and traversal before writing anything."""
    if len(archive) > max_bytes:
        raise GitHubError('snapshot_too_large')
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
            members, size, prefix, seen = [], 0, None, set()
            for member in tar:
                if len(members) >= max_files:
                    raise GitHubError('snapshot_file_limit')
                if not (member.isfile() or member.isdir()):
                    raise GitHubError('unsafe_archive_type')
                raw = member.name.rstrip('/')
                safe_path(destination, raw)
                parts = raw.split('/')
                prefix = prefix or parts[0]
                if parts[0] != prefix:
                    raise GitHubError('multiple_archive_roots')
                relative = '/'.join(parts[1:])
                if not relative:
                    if not member.isdir():
                        raise GitHubError('invalid_archive_root')
                    continue
                safe_path(destination, relative)
                if relative in seen:
                    raise GitHubError('duplicate_archive_path')
                seen.add(relative)
                size += member.size
                if size > max_bytes:
                    raise GitHubError('snapshot_too_large')
                members.append((member, relative))
            for member, relative in members:
                path = safe_path(destination, relative)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, path.open('xb') as dst:
                        shutil.copyfileobj(src, dst)
                    path.chmod(0o600)
    except (tarfile.TarError, ValueError, OSError) as exc:
        raise GitHubError('invalid_snapshot_archive') from None


class GitHubAdapter:
    def __init__(self, transport, app_jwt, cache: Path, consent_check, *, factory=canonical):
        self.transport, self.app_jwt, self.cache = transport, app_jwt, cache.resolve()
        self.consent_check, self.factory = consent_check, factory
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.snapshot_roots = {}
        self.snapshot_hashes = {}
        self.snapshot_repositories = {}
        self.cache_hashes = {}

    def request(self, method, path, token, body=None):
        response = self.transport.request(method, path, token=token, body=body)
        if response.status == 429 or (response.status == 403 and
                (response.headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in response.headers)):
            raise GitHubError('rate_limited')
        if response.status == 401:
            raise GitHubError('credentials_or_installation_revoked')
        if response.status in (403, 404):
            raise GitHubError('installation_or_repository_inaccessible')
        if not 200 <= response.status < 300:
            raise GitHubError('github_transport_error')
        return response.data

    def pages(self, path, token, key):
        items = []
        for page in range(1, 1001):
            data = self.request('GET', f'{path}?per_page=100&page={page}', token)
            batch = data[key]
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise GitHubError('pagination_limit')

    def verify_installation_binding(self, installation_id, user_token, *, state, consume_state):
        # consume_state must atomically bind authenticated user/org/session and expiration.
        if not consume_state(state):
            raise GitHubError('invalid_oauth_state')
        installations = self.pages('/user/installations', user_token, 'installations')
        if str(installation_id) not in [str(item['id']) for item in installations]:
            raise GitHubError('installation_not_accessible_to_authenticated_user')
        installation = self.request('GET', f'/app/installations/{int(installation_id)}', self.app_jwt())
        if installation.get('suspended_at'):
            raise GitHubError('installation_revoked')
        return installation

    def installation_token(self, installation_id, repository_id=None, *, write=False):
        body = {'permissions': {'contents': 'write' if write else 'read'}}
        if write:
            body['permissions']['pull_requests'] = 'write'
        if repository_id is not None:
            body['repository_ids'] = [int(repository_id)]
        data = self.request('POST', f'/app/installations/{int(installation_id)}/access_tokens', self.app_jwt(), body)
        required = body['permissions']
        if any(data.get('permissions', {}).get(k) != v for k, v in required.items()):
            raise GitHubError('missing_installation_permission')
        return data['token']

    def accessible_repositories(self, installation_id):
        token = self.installation_token(installation_id)
        return self.pages('/installation/repositories', token, 'repositories')

    def check_connection(self, connection):
        if connection.status != 'connected' or not connection.scan_consent_at or not self.consent_check(connection):
            raise GitHubError('connection_or_consent_revoked')

    def fetch_repository(self, connection: RepositoryConnection, commit_sha: str) -> RepositorySnapshot:
        self.check_connection(connection)
        if not re.fullmatch('[0-9a-f]{40}', commit_sha):
            raise GitHubError('full_commit_sha_required')
        token = self.installation_token(connection.installation_id, connection.github_repository_id)
        # Recheck live repository access even for cache hits.
        repos = self.pages('/installation/repositories', token, 'repositories')
        repo = next((r for r in repos if str(r['id']) == str(connection.github_repository_id)), None)
        if repo is None:
            raise GitHubError('repository_removed')
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo['full_name']):
            raise GitHubError('invalid_repository_identity')
        commit = self.request('GET', f"/repos/{repo['full_name']}/commits/{commit_sha}", token)
        if commit['sha'] != commit_sha:
            raise GitHubError('commit_mismatch')
        key = f'{int(connection.github_repository_id)}-{commit_sha}'
        root = self.cache / key
        if not root.exists() or self.cache_hashes.get(key) != tree_hash(root):
            data = self.request('GET', f"/repos/{repo['full_name']}/tarball/{commit_sha}", token)
            self.check_connection(connection)
            with tempfile.TemporaryDirectory(dir=self.cache) as temp:
                staged = Path(temp) / 'source'
                staged.mkdir()
                extract_snapshot(data, staged)
                if root.exists():
                    shutil.rmtree(root)
                staged.rename(root)
            self.cache_hashes[key] = tree_hash(root)
        self.check_connection(connection)
        snapshot_id = str(uuid4())
        snapshot_dir = self.cache / 'snapshots'
        snapshot_dir.mkdir(exist_ok=True, mode=0o700)
        copy = snapshot_dir / f'{int(connection.github_repository_id)}-{snapshot_id}'
        try:
            shutil.copytree(root, copy, symlinks=True)
            content_hash = tree_hash(copy)
        except BaseException:
            if copy.exists():
                shutil.rmtree(copy)
            raise
        snapshot = self.factory('RepositorySnapshot', id=snapshot_id, repository_connection_id=connection.id,
            commit_sha=commit_sha, fetched_at=datetime.now(timezone.utc).isoformat(), content_hash=content_hash)
        self.snapshot_roots[snapshot.id] = copy
        self.snapshot_hashes[snapshot.id] = content_hash
        self.snapshot_repositories[snapshot.id] = int(connection.github_repository_id)
        return snapshot

    def snapshot_root(self, snapshot_id: str) -> Path:
        root = self.snapshot_roots[snapshot_id]
        if tree_hash(root) != self.snapshot_hashes[snapshot_id]:
            raise GitHubError('snapshot_changed')
        return root

    def purge_repository(self, repository_id):
        for path in self.cache.glob(f'{int(repository_id)}-*'):
            shutil.rmtree(path)
            self.cache_hashes.pop(path.name, None)
        for snapshot_id, owner in list(self.snapshot_repositories.items()):
            if owner != int(repository_id):
                continue
            root = self.snapshot_roots.pop(snapshot_id)
            if root.exists():
                shutil.rmtree(root)
            self.snapshot_hashes.pop(snapshot_id, None)
            self.snapshot_repositories.pop(snapshot_id, None)
        snapshots = self.cache / 'snapshots'
        if snapshots.is_dir():
            for path in snapshots.glob(f'{int(repository_id)}-*'):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
        if snapshots.is_dir() and not any(snapshots.iterdir()):
            snapshots.rmdir()
