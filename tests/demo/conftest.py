"""Shared helpers for the Stripe demo environment tests (tests/demo).

The consumer templates under demo/templates are standalone repositories with their own test
suites. Tests here never run anything inside demo/templates: ``template_copy`` copies a
template into a temporary directory (with the checkout's installed node_modules), and the
run happens there, so no caches or generated files land in the repository. Subprocesses get
``subprocess_env()``: a minimal environment with the repository venv first on PATH, no Stripe
credentials or API base from the caller, no PYTHONPATH / NODE_OPTIONS / PYTEST_ADDOPTS
leaking in, no bytecode files and a hermetic git configuration.

Share these helpers through the fixtures below: importing ``conftest`` from a test module is
unreliable with several conftest.py files. Test module basenames must stay unique across
tests/* (pytest's default import mode puts each test directory on sys.path).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEMO_DIR = REPO_ROOT / "demo"
TEMPLATES_DIR = DEMO_DIR / "templates"
VENV_BIN = REPO_ROOT / ".venv" / "bin"
VENV_PYTHON = VENV_BIN / "python"
VENV_NODE = VENV_BIN / "node"

# Template directory -> consumer repository name (demo/consumer-repos.manifest.json).
TEMPLATES = {
    "checkout": "shockwave-stripe-checkout",
    "fraud": "shockwave-stripe-fraud",
    "reporting": "shockwave-stripe-reporting",
}
# Never copied from a template: installed packages (copied separately) and local state a run
# in place may have left behind.
COPY_IGNORE = ("node_modules", ".git", "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".DS_Store", "*.sqlite")
MIN_NODE = (22, 18)  # TypeScript type stripping without flags
COMMAND_TIMEOUT_SECONDS = 120


def subprocess_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A minimal, credential-free environment for consumer suites, node and git."""
    env = {
        "PATH": os.pathsep.join((str(VENV_BIN), "/usr/bin", "/bin")),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "NO_COLOR": "1",
        "TZ": "UTC",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    env.update(extra or {})
    return env


def copy_template(template: str, destination: Path, *, node_modules: bool = True) -> Path:
    """Copy ``demo/templates/<template>`` to ``destination`` (which must not exist).

    The tree is copied without caches; the template's installed node_modules (the checkout's
    stripe@23.0.0) follows when ``node_modules`` is true, so its suite runs offline.
    """
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; expected one of {sorted(TEMPLATES)}")
    source = TEMPLATES_DIR / template
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*COPY_IGNORE))
    if node_modules and (source / "package.json").is_file():
        installed = source / "node_modules"
        if not installed.is_dir():
            pytest.skip(
                f"demo/templates/{template}/node_modules is not installed; run `npm ci` there "
                "(PATH=.venv/bin:$PATH) to run its suite"
            )
        shutil.copytree(installed, destination / "node_modules", symlinks=True)
    return destination


def run_command(
    argv: list[str], cwd: Path, *, env: Mapping[str, str] | None = None, timeout: int = COMMAND_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in ``cwd`` with ``subprocess_env(env)``; output is captured as text."""
    return subprocess.run(
        argv, cwd=cwd, env=subprocess_env(env), capture_output=True, text=True, timeout=timeout, check=False
    )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def venv_python() -> Path:
    """The repository venv's Python (stripe==16.0.0 and pytest), used to run the Python consumer suites."""
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    probe = run_command([str(python), "-c", "import pytest, stripe; print(stripe.VERSION)"], REPO_ROOT, timeout=60)
    if probe.returncode != 0:
        pytest.skip(f"{python} cannot import pytest and stripe (install demo/templates/*/requirements.txt)")
    return python


@pytest.fixture(scope="session")
def node_executable() -> Path:
    """The repository venv's Node.js (>= 22.18), used to run the TypeScript consumer suite."""
    node = VENV_NODE if VENV_NODE.exists() else Path(sys.executable).parent / "node"
    if not node.exists():
        pytest.skip(f"Node.js >= {MIN_NODE[0]}.{MIN_NODE[1]} expected at {VENV_NODE}")
    try:
        version = run_command([str(node), "--version"], REPO_ROOT, timeout=60).stdout.strip()
    except OSError:  # e.g. "Bad CPU type in executable"
        version = ""
    match = re.fullmatch(r"v(\d+)\.(\d+)\.\d+", version)
    if match is None or (int(match.group(1)), int(match.group(2))) < MIN_NODE:
        pytest.skip(f"Node.js >= {MIN_NODE[0]}.{MIN_NODE[1]} required, found {version or 'nothing'} at {node}")
    return node


@pytest.fixture(scope="session")
def git_executable() -> str:
    """A git that actually runs here (PATH may list an x86-only /usr/local/bin/git first)."""
    candidates = (shutil.which("git"), "/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git")
    for candidate in dict.fromkeys(item for item in candidates if item):
        try:
            probe = run_command([candidate, "--version"], REPO_ROOT, timeout=60)
        except OSError:  # e.g. "Bad CPU type in executable"
            continue
        if probe.returncode == 0:
            return candidate
    pytest.skip("no runnable git executable found (needed for git init / git apply)")


@pytest.fixture(scope="session", name="subprocess_env")
def subprocess_env_fixture() -> Callable[[Mapping[str, str] | None], dict[str, str]]:
    return subprocess_env


@pytest.fixture(scope="session", name="run_command")
def run_command_fixture() -> Callable[..., subprocess.CompletedProcess[str]]:
    return run_command


@pytest.fixture(scope="session")
def template_copy(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., Path]:
    """Factory: ``template_copy("checkout")`` -> a fresh temporary copy named after its repository."""

    def make(template: str, *, node_modules: bool = True) -> Path:
        destination = tmp_path_factory.mktemp(f"{template}-copy") / TEMPLATES.get(template, template)
        return copy_template(template, destination, node_modules=node_modules)

    return make
