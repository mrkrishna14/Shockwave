"""Narrow, evidence-selected Demo Payments rule. Never global replacement."""
from dataclasses import dataclass
import difflib
import hashlib
import re
from pathlib import Path
from integrations.storage import safe_path


@dataclass(frozen=True)
class ApprovedTarget:
    risk_id: str
    snapshot_id: str
    commit_sha: str
    service_id: str
    file: str
    line: int
    expression: str
    source_sha256: str
    target_contract_snapshot_id: str
    plan: object


def prepare_patch(root: Path, target: ApprovedTarget, *, replacement=None):
    if target.service_id != 'demo-payments':
        raise ValueError('rule_only_supports_demo_payments')
    path = safe_path(root, target.file)
    source = path.read_text()
    if hashlib.sha256(path.read_bytes()).hexdigest() != target.source_sha256:
        raise ValueError('stale_source')
    lines = source.splitlines(keepends=True)
    if not 1 <= target.line <= len(lines):
        raise ValueError('invalid_evidence_line')
    line = lines[target.line - 1]
    if line.count(target.expression) != 1:
        raise ValueError('ambiguous_evidence_expression')
    # Only an approved amount access / 100; origins must be supplied by scanner/backend.
    pattern = r'''([A-Za-z_]\w*(?:\[\s*['"]amount['"]\s*\]|\.amount))\s*/\s*100(?:\.0)?'''
    match = re.fullmatch(pattern, target.expression)
    if not match:
        raise ValueError('unsupported_demo_expression')
    expected = match.group(1)
    if replacement is not None and replacement != expected:
        raise ValueError('model_output_outside_approved_rule_scope')
    lines[target.line - 1] = line.replace(target.expression, expected, 1)
    updated = ''.join(lines)
    diff = ''.join(difflib.unified_diff(source.splitlines(True), updated.splitlines(True),
                                      fromfile='a/' + target.file, tofile='b/' + target.file))
    return {target.file: updated}, diff
