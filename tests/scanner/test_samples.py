"""Committed sample ScanResults must match a fresh scan (stability/golden check)."""
import json

from scanner.contracts import ScanResult
from scanner.samples import generate


def test_samples_match_fresh_scan():
    for name, (svc, root) in generate.CASES.items():
        committed = ScanResult.model_validate(json.loads((generate.OUT / name).read_text()))
        assert generate.build(svc, root) == committed, f"regenerate with: python -m scanner.samples.generate ({name})"
