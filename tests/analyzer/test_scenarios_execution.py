"""Stage C: real native execution of the display scenarios against the consumer doubles.

The development runner double (trusted local fixtures only, NOT a sandbox) copies each
consumer repository, stages the scenario, and runs the native harness with the documented
argv (``python -m pytest`` or ``node --test``). Interpretation then has to reach the
documented verdicts from that real evidence. The fake HTTP layer serves the fixtures (the
demo host is under the reserved .test TLD) and the harnesses' best-effort network guards
refuse the rest in-process; the guard checks below target only localhost and loopback.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from discovery.contracts import Dependency, RepositorySnapshot, Risk, VerificationPlan
from discovery.ids import risk_id
from verification.scenarios import catalog, dev_runner_double, staging
from verification.scenarios.dev_runner_double import run_phase_locally
from verification.scenarios.interpret import (
    PhaseRun,
    interpret_patched_run,
    interpret_verification,
)
from verification.scenarios.plans import PlanConfig, build_verification_plan

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PY_REPO = FIXTURES / "consumer_doubles" / "python_storefront"
TS_REPO = FIXTURES / "consumer_doubles" / "ts_storefront"
NODE = Path(sys.executable).parent / "node"
DEPENDENCIES = json.loads((FIXTURES / "demo_dependencies.json").read_text(encoding="utf-8"))["dependencies"]
CONSUMER_IDS = json.loads((FIXTURES / "consumer_fixture_ids.json").read_text(encoding="utf-8"))
CHANGE_ID = json.loads(
    (REPO_ROOT / "examples/provider/demo_payments/changes/charge_amount_cents_to_dollars.json").read_text(
        encoding="utf-8"
    )
)["id"]
PY_SCENARIO = "demo-payments.charge-amount-units.display.python"
TS_SCENARIO = "demo-payments.charge-amount-units.display.typescript"
DEMO_URL = "http://demo-payments.test/v1/charges/ch_demo"

requires_node = pytest.mark.skipif(not NODE.exists(), reason=f"Node.js >= 22.18 expected at {NODE}")
requires_requests = pytest.mark.skipif(
    importlib.util.find_spec("requests") is None, reason="the Python consumer double imports requests"
)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


SOURCE_DIGESTS = {repo: _tree_digest(repo) for repo in (PY_REPO, TS_REPO)}


def _plan(handle: str, repository: str, scenario_id: str, *, trusted: bool = True) -> VerificationPlan:
    dependency = Dependency.model_validate(DEPENDENCIES[handle])
    pointer = catalog.scenario_pointer_evidence(catalog.get_scenario(scenario_id), dependency)
    risk = Risk(
        id=risk_id(CHANGE_ID, dependency.enrollment_id, dependency.repository_snapshot_id, "conversion"),
        change_id=CHANGE_ID,
        consumer_org_id=dependency.consumer_org_id,
        enrollment_id=dependency.enrollment_id,
        repository_snapshot_id=dependency.repository_snapshot_id,
        dependency_ids=[dependency.id],
        severity="high",
        confidence="direct",
        explanation="Predicted display regression: response.amount is divided by 100.",
        suggested_action="Remove the division once the proposed contract is live.",
        evidence=[*dependency.evidence, pointer],
    )
    ids = CONSUMER_IDS["repositories"][repository]
    snapshot = RepositorySnapshot(
        id=ids["repository_snapshot_id"],
        repository_connection_id=ids["repository_connection_id"],
        commit_sha=ids["commit_sha"],
        fetched_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
        content_hash="sha256:" + "0" * 64,
    )
    config = PlanConfig(frozenset({ids["repository_connection_id"]})) if trusted else PlanConfig()
    return build_verification_plan(risk, snapshot, config=config)


def _edit(relative: str, old: str, new: str) -> Callable[[Path], None]:
    """A patch callable for run_phase_locally; fails loudly if the edit does not apply."""

    def apply(checkout: Path) -> None:
        path = checkout / relative
        text = path.read_text(encoding="utf-8")
        assert old in text, f"patch does not apply to {relative}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    return apply


REMOVE_PYTHON_DIVISION = _edit("storefront/pricing.py", "charge['amount'] / 100", "charge['amount']")
REMOVE_TS_DIVISION = _edit("src/pricing.ts", "(charge.amount / 100)", "(charge.amount)")


def _add_files(files: dict[str, str]) -> Callable[[Path], None]:
    """A patch callable that only adds files; the code under test stays as it is."""

    def apply(checkout: Path) -> None:
        for relative, content in files.items():
            path = checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    return apply


def _all(*patches: Callable[[Path], None]) -> Callable[[Path], None]:
    def apply(checkout: Path) -> None:
        for patch in patches:
            patch(checkout)

    return apply


def _copy_consumer(destination: Path) -> Path:
    shutil.copytree(PY_REPO, destination, ignore=shutil.ignore_patterns("__pycache__"))
    return destination


def _report(run: PhaseRun) -> dict:
    assert isinstance(run.report, bytes), run.execution
    return json.loads(run.report)


@pytest.fixture(scope="module")
def python_plan() -> VerificationPlan:
    return _plan("python_display", "python_storefront", PY_SCENARIO)


@pytest.fixture(scope="module")
def typescript_plan() -> VerificationPlan:
    return _plan("typescript_display", "ts_storefront", TS_SCENARIO)


@pytest.fixture(scope="module")
def python_runs(python_plan: VerificationPlan, tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("python-runs")
    baseline = run_phase_locally(python_plan, PY_REPO, "baseline", work_dir=work)
    proposed = run_phase_locally(python_plan, PY_REPO, "proposed", work_dir=work)
    return work, baseline, proposed


@pytest.fixture(scope="module")
def typescript_runs(typescript_plan: VerificationPlan, tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("typescript-runs")
    baseline = run_phase_locally(typescript_plan, TS_REPO, "baseline", work_dir=work, node_executable=str(NODE))
    proposed = run_phase_locally(typescript_plan, TS_REPO, "proposed", work_dir=work, node_executable=str(NODE))
    return work, baseline, proposed


def _assert_old_pass_new_relevant_fail(plan: VerificationPlan, baseline: PhaseRun, proposed: PhaseRun) -> None:
    old, new = _report(baseline), _report(proposed)
    assert (baseline.execution.exit_code, baseline.execution.tests_run) == (0, 1)
    assert (old["status"], old["expected"], old["actual"], old["fixture_id"]) == (
        "passed",
        "$10.00",
        "$10.00",
        plan.old_fixture_id,
    )
    assert proposed.execution.exit_code not in (0, None) and proposed.execution.tests_run == 1
    assert (new["status"], new["expected"], new["actual"], new["fixture_id"]) == (
        "failed",
        "$10.00",
        "$0.10",
        plan.new_fixture_id,
    )

    result = interpret_verification(plan, baseline, proposed)

    assert result.status == "verified_regression", result.summary
    assert "expected $10.00, actual $0.10" in result.summary
    assert (result.baseline, result.proposed) == (baseline.execution, proposed.execution)


@requires_requests
def test_python_old_pass_new_relevant_fail_is_a_verified_regression(python_plan, python_runs) -> None:
    work, baseline, proposed = python_runs

    _assert_old_pass_new_relevant_fail(python_plan, baseline, proposed)
    for phase in ("baseline", "proposed"):  # the staged checkout stays free of caches
        checkout = work / f"checkout-{phase}"
        assert not (checkout / ".pytest_cache").exists()
        assert not any(checkout.rglob("__pycache__"))


@requires_node
def test_typescript_old_pass_new_relevant_fail_is_a_verified_regression(typescript_plan, typescript_runs) -> None:
    _, baseline, proposed = typescript_runs

    _assert_old_pass_new_relevant_fail(typescript_plan, baseline, proposed)


@requires_requests
@requires_node
def test_fake_http_layer_served_the_fixture_and_recorded_the_demo_url(python_runs, typescript_runs) -> None:
    for scenario_id, runs in ((PY_SCENARIO, python_runs), (TS_SCENARIO, typescript_runs)):
        spec = catalog.get_scenario(scenario_id)
        for run in runs[1:]:
            report = _report(run)
            assert set(report) == set(staging.REPORT_FIELDS)  # both harnesses speak one protocol
            assert report["requested_urls"] == [DEMO_URL]
            assert report["harness_sha256"] == staging.harness_sha256(spec)
            assert report["binding_sha256"] == staging.binding_sha256(spec)


@requires_requests
def test_patched_python_is_validated_with_the_unchanged_expectation(python_plan, tmp_path: Path) -> None:
    patched = run_phase_locally(python_plan, PY_REPO, "patched", work_dir=tmp_path, patch=REMOVE_PYTHON_DIVISION)

    report = _report(patched)
    staged_dir = tmp_path / "checkout-patched" / catalog.get_scenario(PY_SCENARIO).staged_dir
    binding = json.loads((staged_dir / "binding.json").read_text(encoding="utf-8"))
    assert binding["expected_display"] == "$10.00"
    assert (report["fixture_id"], report["expected"], report["actual"]) == (python_plan.new_fixture_id, "$10.00", "$10.00")
    outcome = interpret_patched_run(python_plan, patched)
    assert outcome.status == "validated", outcome.reason
    assert "tested scope" in outcome.reason
    assert "charge['amount'] / 100" in (PY_REPO / "storefront/pricing.py").read_text(encoding="utf-8")


@requires_node
def test_patched_typescript_is_validated_with_the_unchanged_expectation(typescript_plan, tmp_path: Path) -> None:
    patched = run_phase_locally(
        typescript_plan, TS_REPO, "patched", work_dir=tmp_path, node_executable=str(NODE), patch=REMOVE_TS_DIVISION
    )

    report = _report(patched)
    staged_dir = tmp_path / "checkout-patched" / catalog.get_scenario(TS_SCENARIO).staged_dir
    binding = json.loads((staged_dir / "binding.json").read_text(encoding="utf-8"))
    assert binding["expected_display"] == "$10.00"
    assert (report["fixture_id"], report["expected"], report["actual"]) == (
        typescript_plan.new_fixture_id,
        "$10.00",
        "$10.00",
    )
    outcome = interpret_patched_run(typescript_plan, patched)
    assert outcome.status == "validated", outcome.reason
    assert "(charge.amount / 100)" in (TS_REPO / "src/pricing.ts").read_text(encoding="utf-8")


@requires_requests
def test_patched_code_against_the_old_fixture_is_inconclusive(python_plan, tmp_path: Path) -> None:
    """The patch is pinned to the new contract: deployment has to be coordinated."""
    old = run_phase_locally(python_plan, PY_REPO, "baseline", work_dir=tmp_path, patch=REMOVE_PYTHON_DIVISION)
    new = run_phase_locally(python_plan, PY_REPO, "proposed", work_dir=tmp_path, patch=REMOVE_PYTHON_DIVISION)

    assert (_report(old)["status"], _report(old)["actual"]) == ("failed", "$1000.00")
    assert _report(new)["status"] == "passed"
    result = interpret_verification(python_plan, old, new)
    assert result.status == "inconclusive"
    assert "actual $1000.00" in result.summary
    assert "baseline" in result.summary


@requires_requests
@pytest.mark.parametrize(
    ("patch", "error_type"),
    [
        (_edit("storefront/pricing.py", "import os\n", "import os\nimport shockwave_missing_dependency\n"), "ModuleNotFoundError"),
        (_edit("storefront/pricing.py", "def fetch_charge(", "def fetch_charge(:\ndef _unused("), "SyntaxError"),
    ],
    ids=["missing-dependency", "syntax-error"],
)
def test_broken_consumer_code_is_inconclusive(python_plan, python_runs, tmp_path: Path, patch, error_type: str) -> None:
    broken = run_phase_locally(python_plan, PY_REPO, "proposed", work_dir=tmp_path, patch=patch)

    report = _report(broken)
    assert (report["status"], report["error_type"], report["actual"]) == ("error", error_type, None)
    assert broken.execution.exit_code not in (0, None)
    result = interpret_verification(python_plan, python_runs[1], broken)
    assert result.status == "inconclusive"
    assert error_type in result.summary


@requires_requests
def test_timeout_is_inconclusive(python_plan, python_runs, tmp_path: Path) -> None:
    sleep_forever = _edit(
        "storefront/pricing.py",
        "def display_charge_price(charge_id: str) -> str:\n",
        "def display_charge_price(charge_id: str) -> str:\n    import time\n\n    time.sleep(120)\n",
    )

    started = time.monotonic()
    slow = run_phase_locally(python_plan, PY_REPO, "proposed", work_dir=tmp_path, timeout_seconds=3, patch=sleep_forever)
    elapsed = time.monotonic() - started

    assert slow.execution.timed_out and slow.execution.exit_code is None
    assert slow.report is None
    assert elapsed < 30
    result = interpret_verification(python_plan, python_runs[1], slow)
    assert result.status == "inconclusive"
    assert "timed out" in result.summary


@requires_requests
def test_expectation_tampering_is_inconclusive(
    python_plan, python_runs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test "repaired" to expect $0.10 passes against unpatched code; it must prove nothing."""
    original = staging.binding_document
    monkeypatch.setattr(staging, "binding_document", lambda spec: {**original(spec), "expected_display": "$0.10"})
    proposed = run_phase_locally(python_plan, PY_REPO, "proposed", work_dir=tmp_path / "proposed")
    patched = run_phase_locally(python_plan, PY_REPO, "patched", work_dir=tmp_path / "patched")
    monkeypatch.undo()

    staged_dir = tmp_path / "proposed" / "checkout-proposed" / catalog.get_scenario(PY_SCENARIO).staged_dir
    assert json.loads((staged_dir / "binding.json").read_text(encoding="utf-8"))["expected_display"] == "$0.10"
    for run in (proposed, patched):
        report = _report(run)
        assert (report["status"], report["expected"], report["actual"]) == ("passed", "$0.10", "$0.10")
        assert run.execution.exit_code == 0

    result = interpret_verification(python_plan, python_runs[1], proposed)
    assert result.status == "inconclusive"
    assert "expectation tampering" in result.summary
    outcome = interpret_patched_run(python_plan, patched)
    assert outcome.status == "inconclusive"
    assert "expectation tampering" in outcome.reason


@requires_node
def test_typescript_missing_export_is_inconclusive(typescript_plan, typescript_runs, tmp_path: Path) -> None:
    renamed = _edit("src/pricing.ts", "export async function displayChargePrice(", "export async function renderPrice(")

    broken = run_phase_locally(
        typescript_plan, TS_REPO, "proposed", work_dir=tmp_path, node_executable=str(NODE), patch=renamed
    )

    report = _report(broken)
    assert (report["status"], report["error_type"]) == ("error", "TypeError")
    result = interpret_verification(typescript_plan, typescript_runs[1], broken)
    assert result.status == "inconclusive"


# Pytest configuration that rewrites the harness comparison in-process, so the scenario would
# "pass" while storefront/pricing.py still divides by 100 (an honest run displays $0.10).
FORGE_PLUGIN = '''\
import pytest


@pytest.fixture(autouse=True)
def _forge_the_scenario(request):
    original = getattr(request.module, "_run_scenario", None)
    if original is not None:

        def forged(report):
            _, expected = original(report)
            return expected, expected

        request.module._run_scenario = forged
    yield
'''

# Not pytest: a checkout-root module `python -m pytest` would run if the checkout were on
# sys.path, writing a report that passes every integrity check.
SHADOW_PYTEST = '''\
import hashlib
import json
import os
import sys

harness = os.path.abspath(sys.argv[-1])


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


report = {
    "schema_version": "shockwave.v1",
    "kind": "scenario_report",
    "scenario_id": os.environ["SHOCKWAVE_SCENARIO_ID"],
    "phase": os.environ["SHOCKWAVE_SCENARIO_PHASE"],
    "fixture_id": os.environ["SHOCKWAVE_FIXTURE_ID"],
    "assertion_id": "displayed_price_usd",
    "status": "passed",
    "expected": "$10.00",
    "actual": "$10.00",
    "error_type": None,
    "message": "expected $10.00, actual $10.00",
    "requested_urls": ["http://demo-payments.test/v1/charges/ch_demo"],
    "harness_sha256": sha256(harness),
    "binding_sha256": sha256(os.path.join(os.path.dirname(harness), "binding.json")),
}
with open(os.environ["SHOCKWAVE_REPORT_PATH"], "w", encoding="utf-8") as handle:
    json.dump(report, handle)
print("1 passed in 0.01s")
'''

PLUGIN_MODULE = "storefront/pytest_helpers.py"
CONFIG_FORGERIES = [
    pytest.param({"conftest.py": FORGE_PLUGIN}, id="root-conftest"),
    pytest.param({".shockwave/conftest.py": FORGE_PLUGIN}, id="shockwave-conftest"),
    pytest.param(
        {"pytest.ini": "[pytest]\naddopts = -p storefront.pytest_helpers\n", PLUGIN_MODULE: FORGE_PLUGIN},
        id="pytest-ini-addopts-plugin",
    ),
    pytest.param(
        {
            "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-p storefront.pytest_helpers"\n',
            PLUGIN_MODULE: FORGE_PLUGIN,
        },
        id="pyproject-addopts-plugin",
    ),
    pytest.param(
        {
            "forged_helpers-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: forged-helpers\nVersion: 1.0\n",
            "forged_helpers-1.0.dist-info/entry_points.txt": "[pytest11]\nforged = storefront.pytest_helpers\n",
            PLUGIN_MODULE: FORGE_PLUGIN,
        },
        id="entry-point-plugin",
    ),
    pytest.param(
        {"pytest.py": SHADOW_PYTEST},
        id="root-pytest-shadow",
        marks=pytest.mark.skipif(sys.version_info < (3, 11), reason="PYTHONSAFEPATH needs Python >= 3.11"),
    ),
]


@requires_requests
@pytest.mark.parametrize("files", CONFIG_FORGERIES)
def test_a_patch_adding_only_pytest_configuration_cannot_validate(python_plan, tmp_path: Path, files) -> None:
    """The migration must fix the code, not the test (PROMPT Stage C): the run stays honest."""
    patched = run_phase_locally(python_plan, PY_REPO, "patched", work_dir=tmp_path, patch=_add_files(files))

    pricing = (tmp_path / "checkout-patched" / "storefront" / "pricing.py").read_text(encoding="utf-8")
    assert "charge['amount'] / 100" in pricing
    report = _report(patched)
    assert (report["status"], report["expected"], report["actual"]) == ("failed", "$10.00", "$0.10")
    outcome = interpret_patched_run(python_plan, patched)
    assert outcome.status == "validation_failed", outcome.reason


@requires_requests
@pytest.mark.parametrize("files", CONFIG_FORGERIES)
def test_pytest_configuration_in_the_proposed_checkout_cannot_hide_the_regression(
    python_plan, python_runs, tmp_path: Path, files
) -> None:
    proposed = run_phase_locally(python_plan, PY_REPO, "proposed", work_dir=tmp_path, patch=_add_files(files))

    result = interpret_verification(python_plan, python_runs[1], proposed)
    assert result.status == "verified_regression", result.summary


def test_the_double_runs_the_documented_argv_with_the_staged_environment(
    python_plan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def execute(argv: list[str], cwd: Path, env: dict[str, str], timeout: int):
        seen.update(argv=argv, cwd=cwd, env=env)
        return 0, False, 1, "1 passed in 0.01s\n", None

    monkeypatch.setattr(dev_runner_double, "_execute", execute)
    run_phase_locally(python_plan, PY_REPO, "baseline", work_dir=tmp_path)

    assert seen["argv"] == [sys.executable, *staging.runner_argv_hint(python_plan)[1:]]
    assert seen["cwd"] == tmp_path.resolve() / "checkout-baseline"
    assert {key: seen["env"][key] for key in staging.PYTHON_RUNNER_ENV} == staging.PYTHON_RUNNER_ENV


def test_a_consumer_repo_at_or_inside_the_phase_checkout_is_refused_untouched(python_plan, tmp_path: Path) -> None:
    """Every run replaces work_dir/checkout-<phase>, so a source there would be deleted first."""
    work = tmp_path / "work"
    checkout = _copy_consumer(work / "checkout-baseline")
    nested = _copy_consumer(work / "checkout-proposed" / "consumer")
    alias = tmp_path / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    attempts = [("baseline", checkout), ("proposed", nested), ("baseline", alias)]
    if (work / "CHECKOUT-BASELINE").is_dir():  # a case-insensitive volume (the macOS default)
        attempts.append(("baseline", work / "Checkout-Baseline"))
    digests = {source: _tree_digest(source) for source in (checkout, nested)}

    for phase, source in attempts:
        with pytest.raises(ValueError, match=f"checkout-{phase} or inside it"):
            run_phase_locally(python_plan, source, phase, work_dir=work)

    assert {source: _tree_digest(source) for source in (checkout, nested)} == digests


def test_a_work_dir_inside_the_consumer_repo_is_refused(python_plan, tmp_path: Path) -> None:
    source = _copy_consumer(tmp_path / "repo")
    digest = _tree_digest(source)
    work_dirs = [source / "work"]
    if (tmp_path / "REPO").is_dir():  # a case-insensitive volume: another spelling of the same directory
        work_dirs.append(tmp_path / "REPO" / "work")

    for work in work_dirs:
        with pytest.raises(ValueError, match="work_dir must be outside the consumer repository"):
            run_phase_locally(python_plan, source, "baseline", work_dir=work)

    assert _tree_digest(source) == digest and not (source / "work").exists()


def test_a_consumer_repo_beside_the_phase_checkout_is_accepted(tmp_path: Path) -> None:
    plan = _plan("python_display", "python_storefront", PY_SCENARIO, trusted=False)  # refused before copying
    work = tmp_path / "work"

    for source in (_copy_consumer(work / "consumer"), _copy_consumer(work / "checkout-proposed")):
        run = run_phase_locally(plan, source, "baseline", work_dir=work)
        assert run.execution.infrastructure_error and "sandbox" in run.execution.infrastructure_error


GUARD_MESSAGE = "shockwave harness: real network access is disabled"
PYTHON_DISPLAY_DEF = "def display_charge_price(charge_id: str) -> str:\n"
# Prepended to the bound function. Every attempt targets localhost or loopback, so even a
# broken guard sends nothing beyond this machine; a late thread checks the guard outlives the call.
PYTHON_NETWORK_PROBE = '''\
    import socket
    import threading
    import time

    def udp(send):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            send(sock)

    attempts = {
        "gethostbyname": lambda: socket.gethostbyname("localhost"),
        "gethostbyname_ex": lambda: socket.gethostbyname_ex("localhost"),
        "udp-sendto": lambda: udp(lambda sock: sock.sendto(b"x", ("127.0.0.1", 9))),
    }
    if hasattr(socket.socket, "sendmsg"):
        attempts["udp-sendmsg"] = lambda: udp(lambda sock: sock.sendmsg([b"x"], [], 0, ("127.0.0.1", 9)))
    leaked = []
    for name, attempt in attempts.items():
        try:
            attempt()
            leaked.append(name)
        except ConnectionRefusedError as exc:
            if "shockwave harness" not in str(exc):
                leaked.append(name)

    def late_lookup():
        time.sleep(0.5)  # the bound call has returned by now
        try:
            socket.getaddrinfo("localhost", 9)
            outcome = "leaked"
        except ConnectionRefusedError as exc:
            outcome = str(exc)
        with open(os.path.join(os.environ["SHOCKWAVE_REPO_ROOT"], "late-lookup.txt"), "w") as handle:
            handle.write(outcome)

    threading.Thread(target=late_lookup).start()
    if leaked:
        return "leaked: " + ", ".join(leaked)
'''
TS_FIRST_LINE = "// Storefront price display"
TS_IMPORTS = 'import dgram from "node:dgram";\nimport dns, { lookup } from "node:dns";\n'
TS_DISPLAY_DEF = "export async function displayChargePrice(chargeId: string): Promise<string> {\n"
TS_NETWORK_PROBE = """\
  const leaked: string[] = [];
  const resolve = () => {
    const resolver = new dns.Resolver();
    resolver.setServers(["127.0.0.1"]);
    try {
      resolver.resolve4("shockwave.invalid", () => {});
    } finally {
      resolver.cancel();
    }
  };
  const send = () => {
    const socket = dgram.createSocket("udp4");
    try {
      socket.send("x", 9, "127.0.0.1");
    } finally {
      socket.close();
    }
  };
  const attempts: [string, () => unknown][] = [
    ["dns.lookup (named import)", () => lookup("localhost", () => {})],
    ["dns.promises.lookup", () => dns.promises.lookup("localhost").catch(() => {})],
    ["dns.Resolver.resolve4", resolve],
    ["dgram.send", send],
  ];
  for (const [name, attempt] of attempts) {
    try {
      attempt();
      leaked.push(name);
    } catch (error) {
      if (!String((error as Error).message).includes("shockwave harness")) {
        leaked.push(name);
      }
    }
  }
  if (leaked.length > 0) {
    return `leaked: ${leaked.join(", ")}`;
  }
"""


@requires_requests
def test_python_network_guard_refuses_lookups_udp_and_late_threads(python_plan, tmp_path: Path) -> None:
    probe = _edit("storefront/pricing.py", PYTHON_DISPLAY_DEF, PYTHON_DISPLAY_DEF + PYTHON_NETWORK_PROBE)

    run = run_phase_locally(python_plan, PY_REPO, "baseline", work_dir=tmp_path, patch=probe)

    report = _report(run)
    assert (report["status"], report["actual"]) == ("passed", "$10.00"), report["message"]
    late = (tmp_path / "checkout-baseline" / "late-lookup.txt").read_text(encoding="utf-8")
    assert GUARD_MESSAGE in late


@requires_node
def test_node_network_guard_refuses_dns_and_udp(typescript_plan, tmp_path: Path) -> None:
    probe = _all(
        _edit("src/pricing.ts", TS_FIRST_LINE, TS_IMPORTS + TS_FIRST_LINE),
        _edit("src/pricing.ts", TS_DISPLAY_DEF, TS_DISPLAY_DEF + TS_NETWORK_PROBE),
    )

    run = run_phase_locally(
        typescript_plan, TS_REPO, "baseline", work_dir=tmp_path, node_executable=str(NODE), patch=probe
    )

    report = _report(run)
    assert (report["status"], report["actual"]) == ("passed", "$10.00"), report["message"]


@requires_node
def test_node_checkout_configuration_never_reaches_the_run(typescript_plan, typescript_runs, tmp_path: Path) -> None:
    """`node --test <file>` loads no preload from the checkout (the Node side of the hermetic run)."""
    preload = "import { writeFileSync } from 'node:fs';\nwriteFileSync(new URL('./PRELOADED', import.meta.url), 'x');\n"
    configuration = _add_files(
        {
            "node.config.json": '{"nodeOptions": {"import": ["./preload.mjs"]}}\n',
            ".env": "NODE_OPTIONS=--import ./preload.mjs\n",
            "preload.mjs": preload,
        }
    )

    proposed = run_phase_locally(
        typescript_plan, TS_REPO, "proposed", work_dir=tmp_path, node_executable=str(NODE), patch=configuration
    )

    assert not (tmp_path / "checkout-proposed" / "PRELOADED").exists()
    assert interpret_verification(typescript_plan, typescript_runs[1], proposed).status == "verified_regression"


def test_harness_docs_call_the_network_guard_best_effort_not_isolation() -> None:
    for spec in catalog.SCENARIOS.values():
        asset = (catalog.ASSETS_DIR / spec.harness_asset).read_text(encoding="utf-8")
        words = " ".join(asset.replace("//", " ").split())
        assert "best-effort, in-process interception, NOT network isolation" in words
        assert "the runner must isolate the network for every profile" in words
        assert "nothing reaches the real network" not in words


def test_sandbox_profile_is_refused_by_the_double(tmp_path: Path) -> None:
    plan = _plan("python_display", "python_storefront", PY_SCENARIO, trusted=False)
    assert plan.runner_profile == "sandbox_python"

    refused = run_phase_locally(plan, PY_REPO, "baseline", work_dir=tmp_path)

    assert refused.execution.infrastructure_error and "sandbox" in refused.execution.infrastructure_error
    assert refused.report is None
    assert not (tmp_path / "checkout-baseline").exists()
    assert interpret_verification(plan, refused, refused).status == "inconclusive"


def test_missing_node_is_an_infrastructure_error(typescript_plan, tmp_path: Path) -> None:
    run = run_phase_locally(
        typescript_plan, TS_REPO, "baseline", work_dir=tmp_path / "work", node_executable=str(tmp_path / "no-node")
    )

    assert run.execution.infrastructure_error and "node executable not found" in run.execution.infrastructure_error
    assert (run.execution.exit_code, run.execution.tests_run, run.report) == (None, 0, None)
    assert not (tmp_path / "work" / "checkout-baseline").exists()  # fails fast, before copying
    assert interpret_verification(typescript_plan, run, run).status == "inconclusive"


SPEC_REPORTER = chr(0x2139)  # node's default reporter prefixes summary lines with this sign


@pytest.mark.parametrize(
    ("profile", "output", "expected"),
    [
        ("trusted_python", "..F\n1 failed, 2 passed, 1 warning in 0.12s\n", 3),
        ("trusted_python", "==== 1 error in 0.03s ====\n", 1),
        ("trusted_python", "no tests ran in 0.01s\n", 0),
        ("trusted_python", "Ran 4 tests in 0.002s\n\nOK\n", 4),
        ("trusted_python", "Traceback (most recent call last):\n", 0),
        ("trusted_node", "# tests 2\n# pass 2\n", 2),
        ("trusted_node", f"{SPEC_REPORTER} tests 3\n{SPEC_REPORTER} pass 3\n", 3),
        ("trusted_node", "", 0),
    ],
)
def test_tests_run_is_parsed_from_native_summaries(profile: str, output: str, expected: int) -> None:
    assert dev_runner_double._tests_run(profile, output) == expected


def test_captured_output_is_bounded_to_the_tail(tmp_path: Path) -> None:
    script = "print('x' * 300_000); print('1 passed in 0.01s')"

    exit_code, timed_out, _, output, start_error = dev_runner_double._execute(
        [sys.executable, "-c", script], tmp_path, {"PATH": "/usr/bin:/bin"}, 30
    )

    assert (exit_code, timed_out, start_error) == (0, False, None)
    assert len(output.encode("utf-8")) <= dev_runner_double.MAX_OUTPUT_BYTES
    assert output.rstrip().endswith("1 passed in 0.01s")


def test_consumer_doubles_were_never_mutated() -> None:
    assert {repo: _tree_digest(repo) for repo in (PY_REPO, TS_REPO)} == SOURCE_DIGESTS
