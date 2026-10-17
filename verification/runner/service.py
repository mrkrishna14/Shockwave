"""Native tests for registered team fixtures. Host timeout is NOT a sandbox."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from shared.schemas import VerificationPlan, VerificationResult

from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from uuid import uuid4
import xml.etree.ElementTree as ET
from integrations.contracts import canonical
from integrations.storage import safe_path, tree_hash
from .profiles import command


@dataclass
class RunEvidence:
    result: object
    relevant_failure: bool = False
    assertion_seen: bool = False


def passed(result):
    return (result is not None and result.exit_code == 0 and result.tests_run > 0
            and not result.timed_out and result.infrastructure_error is None)


class RunnerService:
    def __init__(self, artifacts, snapshots, scenarios, *, factory=canonical, python=None,
                 consent_check=lambda snapshot_id: False, max_output=65536):
        self.artifacts, self.snapshots, self.scenarios = artifacts, snapshots, scenarios
        self.factory, self.python, self.consent_check = factory, python, consent_check
        self.max_output = max_output

    def _error(self, error):
        return RunEvidence(self.factory('ExecutionResult', exit_code=None, timed_out=False,
            infrastructure_error=error, artifact_ids=[], duration_ms=0, tests_run=0))

    def execute(self, plan, repo_root: Path, proposed: bool, *, edits=None):
        """edits is a backend-only validated patch map; source tests cannot be edited."""
        try:
            if not self.consent_check(plan.repository_snapshot_id):
                return self._error('consent_revoked')
            if plan.runner_profile.startswith('sandbox_'):
                return self._error('sandbox_unavailable: untrusted execution is blocked')
            snapshot = self.snapshots[plan.repository_snapshot_id]
            scenario = self.scenarios[plan.scenario_id]
            if (repo_root.resolve() != snapshot.root or tree_hash(repo_root) != snapshot.content_hash):
                return self._error('untrusted_or_stale_source')
            if (tuple(plan.test_files) != scenario.test_files or
                plan.old_fixture_id != scenario.old_fixture_id or plan.new_fixture_id != scenario.new_fixture_id):
                return self._error('unapproved_scenario')
            if not 0 < plan.timeout_seconds <= 120:
                return self._error('invalid_timeout')
            with tempfile.TemporaryDirectory(prefix='shockwave-run-') as temp:
                work = Path(temp) / 'repo'
                shutil.copytree(repo_root, work)
                for relative, content in (edits or {}).items():
                    if relative in scenario.test_files:
                        return self._error('test_mutation_rejected')
                    safe_path(work, relative).write_text(content)
                for relative in plan.test_files:
                    if not safe_path(work, relative).is_file():
                        return self._error('test_file_missing')
                report = Path(temp) / 'report.xml'
                kwargs = {'python': self.python} if self.python else {}
                argv = command(plan.runner_profile, plan.test_files, report, **kwargs)
                env = {'PATH': os.defpath, 'HOME': temp, 'TMPDIR': temp,
                       'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
                       'SW_RESPONSE': json.dumps(scenario.new_response if proposed else scenario.old_response)}
                started = time.monotonic()
                proc = subprocess.Popen(argv, cwd=work, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                output = bytearray()
                timed_out, overflow = False, False
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        if time.monotonic() - started >= plan.timeout_seconds:
                            timed_out = True
                            break
                        for key, _ in selector.select(0.03):
                            chunk = os.read(key.fd, 8192)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            else:
                                remaining = self.max_output - len(output)
                                output.extend(chunk[:remaining])
                                if len(chunk) > remaining:
                                    overflow = True
                        if overflow:
                            break
                if not timed_out and not overflow:
                    try:
                        proc.wait(timeout=max(0.01, plan.timeout_seconds - (time.monotonic() - started)))
                    except subprocess.TimeoutExpired:
                        timed_out = True
                # Best-effort descendant cleanup for trusted fixtures; this is not isolation.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    if proc.poll() is None:
                        proc.kill()
                proc.wait()
                proc.stdout.close()
                duration = int((time.monotonic() - started) * 1000)
                artifact = self.artifacts.put(bytes(output))
                error = 'output_limit_exceeded' if overflow else None
                tests_run, relevant, seen = 0, False, False
                if not timed_out and not error:
                    try:
                        data = report.read_bytes() if plan.runner_profile == 'trusted_python' else bytes(output)
                        if len(data) > self.max_output:
                            raise ValueError('report_limit_exceeded')
                        if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
                            raise ValueError('unsafe_report')
                        xml = ET.fromstring(data)
                        for case in xml.iter('testcase'):
                            if case.find('skipped') is not None:
                                continue
                            tests_run += 1
                            is_target = case.get('name') in scenario.assertion_names
                            seen |= is_target
                            if case.find('error') is not None:
                                error = 'test_infrastructure_error'
                            for failure in case.findall('failure'):
                                detail = ' '.join(failure.itertext()) + str(failure.attrib)
                                relevant |= is_target and ('AssertionError' in detail or 'ERR_ASSERTION' in detail or '\nE       assert ' in detail)
                        if proc.returncode not in (0, 1):
                            error = 'runner_exit_error'
                    except (OSError, ET.ParseError, ValueError):
                        error = 'missing_or_invalid_test_report'
                result = self.factory('ExecutionResult', exit_code=proc.returncode,
                    timed_out=timed_out, infrastructure_error=error, artifact_ids=[artifact],
                    duration_ms=duration, tests_run=tests_run)
                return RunEvidence(result, relevant, seen)
        except (KeyError, ValueError, OSError):
            return self._error('invalid_runner_configuration_or_source')

    def verify_regression(self, plan: VerificationPlan, repo_root: Path) -> VerificationResult:
        baseline = self.execute(plan, repo_root, False)
        proposed = self.execute(plan, repo_root, True) if passed(baseline.result) and baseline.assertion_seen else None
        status = 'inconclusive'
        if proposed and proposed.assertion_seen:
            if passed(proposed.result):
                status = 'compatible_within_tested_scope'
            elif (proposed.result.exit_code == 1 and proposed.relevant_failure
                  and not proposed.result.timed_out and not proposed.result.infrastructure_error):
                status = 'verified_regression'
        return self.factory('VerificationResult', schema_version='shockwave.v1', id=str(uuid4()),
            risk_id=plan.risk_id, repository_snapshot_id=plan.repository_snapshot_id,
            status=status, baseline=baseline.result, proposed=proposed.result if proposed else None,
            scenario_id=plan.scenario_id, summary=f'{status}; only the registered scenario scope was tested.',
            artifact_ids=baseline.result.artifact_ids + (proposed.result.artifact_ids if proposed else []))
