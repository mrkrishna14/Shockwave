"""Commands and trust registration are internal backend configuration, never HTTP input."""
from dataclasses import dataclass
from pathlib import Path
import shutil
import sys
from integrations.storage import tree_hash


@dataclass(frozen=True)
class TrustedSnapshot:
    root: Path
    content_hash: str
    commit_sha: str

    @classmethod
    def register_team_fixture(cls, root: Path, commit_sha: str):
        return cls(root.resolve(), tree_hash(root), commit_sha)


@dataclass(frozen=True)
class Scenario:
    id: str
    old_fixture_id: str
    new_fixture_id: str
    old_response: dict
    new_response: dict
    test_files: tuple[str, ...]
    assertion_names: frozenset[str]
    target_contract_snapshot_id: str


def command(profile: str, tests: list[str], report: Path, python: str = sys.executable):
    if profile == 'trusted_python':
        return [python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                f'--junitxml={report}', '--', *tests]
    if profile == 'trusted_node':
        node = shutil.which('node')
        if not node:
            raise ValueError('node_unavailable')
        return [node, '--test', '--test-reporter=junit', *tests]
    if profile in ('sandbox_python', 'sandbox_node'):
        raise ValueError('sandbox_unavailable: untrusted execution is blocked')
    raise ValueError('unknown_runner_profile')
