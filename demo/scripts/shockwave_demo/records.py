"""Run records: what was executed, with which inputs, and what it showed.

Every executed test run (Shockwave scenario phases, controls, consumers' own suites, migration
checks) gets one record, written twice:

* ``results/runs/<run_id>.json`` - normalized and deterministic (duration_ms 0, no log text),
  part of the reference results compared byte for byte with demo/expected/results;
* ``runs/<run_id>.json`` - the same record with the measured duration and a bounded (<= 4 KB),
  redacted log tail.

Shape (kind ``shockwave-demo.run-record@1``): run_id, stage, purpose, runner, repository,
commit_sha, scenario_id, phase, fixture {fixture_set_id, sha256, ...}, command {argv, cwd},
exit_code, timed_out, infrastructure_error, tests_run, outcome, reason, assertions [case
evidence: case_id, role, expected, actual, status, detail, requests], duration_ms.
"""

from __future__ import annotations

from collections.abc import Mapping

from shockwave_demo.common import TIMING_NOTE, Layout, log_tail, write_json

RUN_RECORD_KIND = "shockwave-demo.run-record@1"


def write_run_record(layout: Layout, record: Mapping, output: str | None, measured_ms: int | None = None) -> dict:
    normalized = {"kind": RUN_RECORD_KIND, **record, "duration_ms": 0, "timing": TIMING_NOTE}
    write_json(layout.result_path(f"runs/{record['run_id']}.json"), normalized)
    raw = {
        **normalized,
        "duration_ms": measured_ms if measured_ms is not None else record.get("duration_ms", 0),
        "timing": "measured duration of this run (wall clock)",
        "log_tail": log_tail(output, layout),
    }
    write_json(layout.raw_runs_dir / f"{record['run_id']}.json", raw)
    return normalized
