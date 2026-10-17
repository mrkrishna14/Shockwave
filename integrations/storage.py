"""Internal private artifacts and strict repository-relative file handling."""
import hashlib
import json
from pathlib import Path, PurePosixPath
from uuid import uuid4


def safe_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (not relative or '\\' in relative or '\x00' in relative or
            relative.startswith('/') or any(p in ('..', '.git') for p in parts)
            or ':' in relative or relative != str(PurePosixPath(relative))):
        raise ValueError('unsafe_repository_path')
    root = root.resolve()
    candidate = root.joinpath(*parts)
    for p in [candidate, *candidate.parents]:
        if p == root:
            break
        if p.is_symlink():
            raise ValueError('symlink_rejected')
    if not candidate.resolve().is_relative_to(root):
        raise ValueError('path_escape')
    return candidate


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if '.git' in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise ValueError('symlink_rejected')
        if path.is_file():
            digest.update(relative.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class ArtifactStore:
    """Backend must authorize downloads. Paths are never returned through contracts."""
    def __init__(self, root: Path, limit: int = 128 * 1024):
        self.root, self.limit = root.resolve(), limit
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put(self, value: bytes) -> str:
        if len(value) > self.limit:
            raise ValueError('artifact_too_large')
        key = str(uuid4())
        path = self.root / key
        path.touch(mode=0o600)
        path.write_bytes(value)
        return key

    def get(self, key: str) -> bytes:
        return safe_path(self.root, key).read_bytes()

    def put_json(self, value) -> str:
        return self.put(json.dumps(value, indent=2).encode())
