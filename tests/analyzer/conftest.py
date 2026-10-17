"""Test setup: import packages from the repository root, never from this test directory.

Test module basenames must be unique across tests/discovery and tests/analyzer
(pytest's default import mode puts each test directory on sys.path).
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Fixture trees (provider/consumer doubles, specs) are data, not test modules.
collect_ignore_glob = ["fixtures/*"]
