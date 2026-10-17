"""Generate in execution copies, validate unchanged tests, retain private receipts."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from shared.schemas import MigrationRequest, Risk, VerificationResult, MigrationResult

from dataclasses import dataclass
import hashlib
from pathlib import Path
from uuid import uuid4
from integrations.contracts import canonical
from integrations.storage import tree_hash
from verification.runner.service import passed
from .patches import prepare_patch


@dataclass(frozen=True)
class ValidatedPatch:
    migration_id: str
    snapshot_id: str
    commit_sha: str
    target_contract_snapshot_id: str
    edits: dict
    patch_artifact_id: str
    validation_artifact_ids: tuple


class MigrationService:
    def __init__(self, runner, artifacts, targets, *, factory=canonical,
                 consent_check=lambda snapshot_id: False, model=None, allow_model=lambda risk: False):
        self.runner, self.artifacts, self.targets = runner, artifacts, targets
        self.factory, self.consent_check, self.model, self.allow_model = factory, consent_check, model, allow_model
        self.validated = {}

    def generate_migration(self, request: MigrationRequest, risk: Risk, verification: VerificationResult, repo_root: Path) -> MigrationResult:
        fields = dict(schema_version='shockwave.v1', id=str(uuid4()), risk_id=risk.id,
            repository_snapshot_id=request.repository_snapshot_id,
            target_contract_snapshot_id=request.target_contract_snapshot_id,
            status='blocked', generation_mode='model' if self.model else 'rule',
            patch_artifact_id=None, changed_files=[], validation=None, pr_url=None, branch=None,
            warnings=['Targets the proposed contract only; deployment must be coordinated. Old responses are not supported by this migration.'])
        try:
            target = self.targets[risk.id]
            snapshot = self.runner.snapshots[request.repository_snapshot_id]
            if (request.mode != 'preview' or request.risk_id != risk.id or verification.risk_id != risk.id or
                request.verification_id != verification.id or verification.status != 'verified_regression' or
                request.repository_snapshot_id != risk.repository_snapshot_id or
                request.repository_snapshot_id != verification.repository_snapshot_id or
                target.snapshot_id != request.repository_snapshot_id or target.risk_id != risk.id or
                target.plan.risk_id != risk.id or target.plan.repository_snapshot_id != target.snapshot_id or
                target.plan.scenario_id != verification.scenario_id or
                request.target_contract_snapshot_id != target.target_contract_snapshot_id or
                target.target_contract_snapshot_id != self.runner.scenarios[target.plan.scenario_id].target_contract_snapshot_id or
                target.commit_sha != snapshot.commit_sha or snapshot.root != repo_root.resolve() or
                tree_hash(repo_root) != snapshot.content_hash):
                raise ValueError('mismatched_or_stale_migration_context')
            if not self.consent_check(target.snapshot_id):
                raise ValueError('consent_revoked')
            for attempt in range(2 if self.model else 1):
                if not self.consent_check(target.snapshot_id):
                    raise ValueError('consent_revoked')
                replacement = None
                if self.model:
                    if not self.allow_model(risk):
                        raise ValueError('model_processing_not_authorized')
                    # Runtime adapter receives only approved expression, no repo instructions/secrets.
                    replacement = self.model(expression=target.expression, target='amount in USD dollars', attempt=attempt + 1)
                edits, diff = prepare_patch(repo_root, target, replacement=replacement)
                fields['patch_artifact_id'] = self.artifacts.put(diff.encode())
                fields['changed_files'] = list(edits)
                outcome = self.runner.execute(target.plan, repo_root, True, edits=edits)
                fields['validation'] = outcome.result
                if passed(outcome.result) and outcome.assertion_seen:
                    fields['status'] = 'patch_validated'
                    self.validated[fields['id']] = ValidatedPatch(fields['id'], target.snapshot_id,
                        target.commit_sha, target.target_contract_snapshot_id, edits,
                        fields['patch_artifact_id'], tuple(outcome.result.artifact_ids))
                    break
                fields['status'] = 'validation_failed'
                fields['warnings'].append('Failed attempt artifacts: ' + ','.join(outcome.result.artifact_ids))
        except (KeyError, ValueError, OSError):
            fields['warnings'].append('Blocked: invalid context, stale source, missing consent, or unsupported patch scope.')
        return self.factory('MigrationResult', **fields)
