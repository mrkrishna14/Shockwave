"""Unit tests for demo/scripts/shockwave_demo: fixture server, reset, live-check guards, scan drift, logs.

Nothing here touches the network, Stripe or the repository's demo/.work and demo/reports.
live-check is exercised only through its refusal paths and argument handling.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "demo" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from shockwave_demo import common, live_check, reset, scan
from shockwave_demo.common import DemoError, Layout
from shockwave_demo.fixture_server import RequestLog, make_server


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# Fixture server ------------------------------------------------------------------------


def _get(url: str, method: str = "GET", headers: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.mark.parametrize(("variant", "amount"), [("baseline", 1000), ("proposed", 10)])
def test_fixture_server_serves_the_simulated_responses(variant: str, amount: int) -> None:
    log = RequestLog()
    server = make_server(variant, 0, log=log)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        base = f"http://{host}:{port}"
        headers = {"Authorization": "Bearer sk_test_unit", "Stripe-Version": "2026-09-30.endive"}
        status, body = _get(f"{base}/v1/payment_intents/pi_demo_10usd", headers=headers)
        assert (status, body["amount"], body["currency"], body["object"]) == (200, amount, "usd", "payment_intent")
        status, body = _get(f"{base}/v1/payment_intents/pi_unknown")
        assert (status, body["error"]["code"], body["error"]["type"]) == (404, "resource_missing", "invalid_request_error")
        status, _ = _get(f"{base}/v1/payment_intents/pi_demo_10usd", method="POST")
        assert status == 405
        status, _ = _get(f"{base}/v1/charges/ch_1")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
    assert log.entries[0] == {
        "method": "GET",
        "path": "/v1/payment_intents/pi_demo_10usd",
        "stripe_version": "2026-09-30.endive",
        "authorization": "test_mode_bearer",
        "status": 200,
    }
    assert "sk_test_unit" not in json.dumps(log.entries)


def test_fixture_server_command_is_labeled_and_loopback_only(subprocess_env) -> None:
    process = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "demo.py"), "fixture-server", "--variant", "proposed", "--port", "0", "--max-requests", "1"],
        cwd=REPO_ROOT,
        env=subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        lines = []
        while True:
            line = process.stdout.readline()
            assert line, "".join(lines)
            lines.append(line)
            if "listening" in line:
                url = line.split()[-1]
                break
        assert url.startswith("http://127.0.0.1:")
        status, body = _get(f"{url}/v1/payment_intents/pi_demo_6000usd")
        assert (status, body["amount"]) == (200, 6000)
        output = "".join(lines) + process.communicate(timeout=30)[0]
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0
    assert "a simulation of Stripe responses" in output and "not Stripe" in output
    assert "SIMULATED change: hypothetical, not announced by Stripe" in output


# Reset ---------------------------------------------------------------------------------


def test_reset_deletes_only_known_generated_entries(tmp_path: Path) -> None:
    work, reports = tmp_path / "work", tmp_path / "reports"
    for path in (work / "repos" / "x", work / "runs", reports / "results" / "runs", reports / "runs", reports / "sources"):
        path.mkdir(parents=True)
    (work / "snapshots.json").write_text("{}", encoding="utf-8")
    (work / "mine.txt").write_text("keep", encoding="utf-8")
    for name in ("README.md", ".gitkeep", "notes.txt"):
        (reports / name).write_text("keep", encoding="utf-8")
    layout = Layout(work_dir=work, reports_dir=reports)
    dry = reset.run_reset(layout, dry_run=True)
    assert sum("would delete" in line for line in dry) == 6
    assert (work / "repos").is_dir()
    reset.run_reset(layout)
    assert sorted(path.name for path in work.iterdir()) == ["mine.txt"]
    assert sorted(path.name for path in reports.iterdir()) == [".gitkeep", "README.md", "notes.txt"]


def test_reset_defaults_target_only_demo_work_and_demo_reports() -> None:
    layout = Layout()
    for path in (layout.work_dir / name for name in reset.WORK_ENTRIES):
        assert path.parent == REPO_ROOT / "demo" / ".work"
    for path in (layout.reports_dir / name for name in reset.REPORT_ENTRIES):
        assert path.parent == REPO_ROOT / "demo" / "reports"
    assert not set(reset.REPORT_ENTRIES) & set(reset.KEPT_REPORT_FILES)


def test_remove_tree_refuses_paths_outside_the_allowed_root(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(DemoError, match="outside"):
        common.remove_tree(tmp_path / "b", inside=tmp_path / "a")
    assert (tmp_path / "b").is_dir()


# live-check guards ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("authorized", "key", "payment_intent", "create", "message"),
    [
        (False, "sk_test_abc123", "pi_123456789", False, "--authorize-live-test-mode"),
        (True, None, "pi_123456789", False, "is not set"),
        (True, "sk_live_abc123", "pi_123456789", False, "LIVE-mode key"),
        (True, "rk_live_abc123", "pi_123456789", False, "LIVE-mode key"),
        (True, "pk_test_abc123", "pi_123456789", False, "starting with sk_test_"),
        (True, "rk_test_abc123", "pi_123456789", False, "starting with sk_test_"),
        (True, "sk_test_abc123", None, False, "--payment-intent pi_"),
        (True, "sk_test_abc123", "pi_123456789", True, "not both"),
        (True, "sk_test_abc123", "ch_123456789", False, "must look like pi_"),
        (True, "sk_test_abc123", "pi_12;rm", False, "must look like pi_"),
    ],
)
def test_live_check_refuses_before_any_network_access(
    no_network, tmp_path: Path, authorized, key, payment_intent, create, message
) -> None:
    environ = {} if key is None else {"STRIPE_SECRET_KEY": key}
    layout = Layout(work_dir=tmp_path / "work", reports_dir=tmp_path / "reports")
    with pytest.raises(live_check.LiveCheckRefused, match=message) as refused:
        live_check.run_live_check(layout, authorized=authorized, payment_intent=payment_intent, create=create, environ=environ)
    if key:
        assert key not in str(refused.value)
    assert not (tmp_path / "work").exists()


def test_live_check_cli_refusal_exits_2_without_printing_the_key(no_network, monkeypatch, capsys, tmp_path: Path) -> None:
    from shockwave_demo.cli import main

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_do_not_print_me")
    code = main(["live-check", "--authorize-live-test-mode", "--payment-intent", "pi_123456789", "--work-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "REFUSED" in captured.err and "LIVE-mode key" in captured.err
    assert "sk_live_do_not_print_me" not in captured.out + captured.err
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    assert main(["live-check", "--work-dir", str(tmp_path)]) == 2
    assert "cannot exercise the hypothetical change" in capsys.readouterr().err


def test_live_check_create_parameters_exist_in_the_pinned_spec(no_network) -> None:
    assert live_check.create_parameter_problems(live_check.CREATE_PARAMETERS) == []
    assert live_check.CREATE_PARAMETERS["amount"] == 1000 and live_check.CREATE_PARAMETERS["currency"] == "usd"
    assert live_check.CREATE_PARAMETERS["payment_method"] == "pm_card_visa"
    assert live_check.create_parameter_problems(["amount", "not_a_parameter"]) == [
        "not_a_parameter is not a POST /v1/payment_intents parameter in the pinned spec"
    ]
    # payment_method_types is not a create parameter in the pinned API version; nested names are checked too.
    assert live_check.create_parameter_problems({"payment_method_types": ["card"]}) == [
        "payment_method_types is not a POST /v1/payment_intents parameter in the pinned spec"
    ]
    assert live_check.create_parameter_problems({"automatic_payment_methods": {"enabled": True, "bogus": 1}}) == [
        "automatic_payment_methods[bogus] is not a POST /v1/payment_intents parameter in the pinned spec"
    ]


def test_live_check_accepts_a_test_key_and_a_payment_intent_without_contacting_stripe(no_network) -> None:
    key = live_check.check_request(
        authorized=True, payment_intent="pi_3QabcDEF123", create=False, environ={"STRIPE_SECRET_KEY": "sk_test_abc"}
    )
    assert key == "sk_test_abc"


# Scan drift ----------------------------------------------------------------------------


def _prepared_copy(tmp_path: Path, template: str, name: str) -> tuple[Path, str]:
    repo = tmp_path / name
    shutil.copytree(REPO_ROOT / "demo" / "templates" / template, repo, ignore=shutil.ignore_patterns(*common.COPY_IGNORE))
    common.git(["init", "-q", "--template=", "-b", "main"], repo)
    common.git(["add", "-A"], repo)
    common.git(["commit", "-q", "-m", "snapshot"], repo)
    return repo, common.git(["rev-parse", "HEAD"], repo).strip()


def test_scan_binds_fixture_evidence_to_the_prepared_commit(tmp_path: Path, git_executable) -> None:
    fixture = common.read_json(common.DEPENDENCY_FIXTURE_PATH)
    ids = common.read_json(common.IDS_PATH)
    repo, commit = _prepared_copy(tmp_path, "fraud", "shockwave-stripe-fraud")
    snapshot_id = ids["consumers"]["shockwave-stripe-fraud"]["repository_snapshot_id"]
    records, controls, checked = scan.bind_fixture_records(fixture, "shockwave-stripe-fraud", commit, repo, snapshot_id)
    assert len(records) == 3 and checked > 0 and [item["id"] for item in controls] == ["fraud_velocity_limit_amount"]
    revisions = {item.revision for record in records for item in record.evidence if item.kind == "source"}
    assert revisions == {commit}


def test_scan_refuses_to_bind_when_a_template_line_drifted(tmp_path: Path, git_executable) -> None:
    fixture = common.read_json(common.DEPENDENCY_FIXTURE_PATH)
    ids = common.read_json(common.IDS_PATH)
    repo, _ = _prepared_copy(tmp_path, "fraud", "shockwave-stripe-fraud")
    review = repo / "fraud_service" / "review.py"
    review.write_text(review.read_text(encoding="utf-8").replace("> 500000", "> 499999"), encoding="utf-8")
    common.git(["commit", "-q", "-am", "drift"], repo)
    commit = common.git(["rev-parse", "HEAD"], repo).strip()
    snapshot_id = ids["consumers"]["shockwave-stripe-fraud"]["repository_snapshot_id"]
    with pytest.raises(DemoError, match=r"fixture drift .*fraud_service/review.py:24"):
        scan.bind_fixture_records(fixture, "shockwave-stripe-fraud", commit, repo, snapshot_id)


# Logs and command records --------------------------------------------------------------


def test_log_tail_is_bounded_and_redacted(tmp_path: Path) -> None:
    layout = Layout(work_dir=tmp_path / "work", reports_dir=tmp_path / "reports")
    text = (
        "x" * 10000
        + f"\nkey sk_test_abcdefghijklmnop and rk_live_123456 in {tmp_path}/work/runs/a\n"
        + "Authorization: Bearer sk_live_zzzzzz\n"
    )
    tail = common.log_tail(text, layout)
    assert tail["truncated"] is True and tail["bytes_kept"] <= 4096
    assert "sk_test_abc" not in tail["text"] and "rk_live_" not in tail["text"] and "sk_live_" not in tail["text"]
    assert "$WORK_DIR/runs/a" in tail["text"] and str(tmp_path) not in tail["text"]
    assert tail["redactions"] >= 3


def test_suite_counts_parse_node_and_pytest_output() -> None:
    node = (
        "✔ a (1ms)\n✖ order summary shows the total (2ms)\nℹ tests 4\nℹ pass 3\nℹ fail 1\n"
        "\n✖ failing tests:\n\ntest at test/x.ts:1:1\n✖ order summary shows the total (2.5ms)\n"
    )
    assert common.suite_counts("node", node) == {
        "tests": 4,
        "passed": 3,
        "failed": 1,
        "failing_tests": ["order summary shows the total"],
    }
    pytest_output = (
        "FAILED tests/test_reporting.py::test_ledger_records_stripe_amounts - AssertionError: x\n"
        "1 failed, 2 passed in 0.31s\n"
    )
    assert common.suite_counts("pytest", pytest_output) == {
        "tests": 3,
        "passed": 2,
        "failed": 1,
        "failing_tests": ["tests/test_reporting.py::test_ledger_records_stripe_amounts"],
    }


def test_display_argv_never_shows_absolute_paths(tmp_path: Path) -> None:
    layout = Layout(work_dir=tmp_path / "work", reports_dir=tmp_path / "reports")
    shown = common.display_argv([sys.executable, "-m", "pytest", str(tmp_path / "work" / "x.py"), str(REPO_ROOT / "demo")], layout)
    assert shown == [Path(sys.executable).name, "-m", "pytest", "$WORK_DIR/x.py", "demo"]


def test_own_suite_command_follows_the_consumer_manifest() -> None:
    manifest = {entry["name"]: entry for entry in common.read_json(common.CONSUMER_MANIFEST_PATH)["repositories"]}
    checkout = common.own_suite_command(manifest["shockwave-stripe-checkout"], REPO_ROOT / "demo" / "templates" / "checkout", "/x/node")
    assert checkout.argv == ["/x/node", "--test", "test/**/*.test.ts"] and checkout.shown[0] == "node"
    fraud = common.own_suite_command(manifest["shockwave-stripe-fraud"], REPO_ROOT / "demo" / "templates" / "fraud", "node")
    assert fraud.shown == ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


# fetch-sources (offline part only) ------------------------------------------------------


def test_offline_source_checks_all_pass_without_network(no_network) -> None:
    from shockwave_demo import sources

    checks = sources.offline_checks()
    assert [item["check"] for item in checks if not item["ok"]] == []
    names = {item["check"] for item in checks}
    assert {"lock consistency", "vendored OpenAPI copy", "venv stripe-python"} <= names


def test_lock_paths_exclude_api_names() -> None:
    from shockwave_demo import sources

    lock = common.read_json(common.LOCK_PATH)
    python = next(item for item in lock["sources"] if item["key"] == "stripe-python")
    assert sources._source_paths(python) == ["stripe/_api_version.py", "stripe/_payment_intent.py"]


# Scanner integration guard -----------------------------------------------------------------


def test_an_importable_scanner_without_the_documented_interface_is_refused(monkeypatch) -> None:
    import importlib.machinery
    import types

    fake = types.ModuleType("scanner")
    fake.__spec__ = importlib.machinery.ModuleSpec("scanner", None)
    monkeypatch.setitem(sys.modules, "scanner", fake)
    with pytest.raises(DemoError, match="--use-fixture"):
        scan._scanner()
    fake.scan_repository = lambda *args, **kwargs: []
    assert scan._scanner() is fake.scan_repository


def test_without_a_scanner_package_the_fixture_adapter_is_used() -> None:
    assert "scanner" not in sys.modules
    assert scan._scanner() is None
    assert scan.SCAN_FIXTURE_LABEL == "fixture (Noirit scanner not integrated)"


@pytest.mark.parametrize(("variant", "flagged"), [("baseline", True), ("proposed", False)])
def test_fixture_server_answers_the_real_stripe_python_sdk(variant: str, flagged: bool, template_copy, run_command) -> None:
    """The fraud consumer's own code (stripe-python 16) against the standalone server: USD $6,000.00."""
    repo = template_copy("fraud", node_modules=False)
    log = RequestLog()
    server = make_server(variant, 0, log=log)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        code = (
            "from fraud_service.review import review_payment\n"
            "print(review_payment('pi_demo_6000usd').needs_manual_review)\n"
        )
        completed = run_command(
            [sys.executable, "-c", code],
            repo,
            env={"STRIPE_SECRET_KEY": "sk_test_offline_unit", "STRIPE_API_BASE": f"http://{host}:{port}"},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == str(flagged)
    assert [(item["method"], item["path"], item["stripe_version"], item["authorization"]) for item in log.entries] == [
        ("GET", "/v1/payment_intents/pi_demo_6000usd", "2026-09-30.endive", "test_mode_bearer")
    ]


def test_rerunning_a_stage_removes_only_downstream_outputs(tmp_path: Path) -> None:
    from shockwave_demo.results import invalidate_downstream

    layout = Layout(work_dir=tmp_path / "work", reports_dir=tmp_path / "reports")
    files = [
        "results/snapshots.json",
        "results/consumers/r/dependencies.json",
        "results/consumers/r/findings.json",
        "results/change/analysis.json",
        "results/verification/results.json",
        "results/migrations/validation.json",
        "results/runs/prepare.r.own-suite.json",
        "results/runs/execute.x.baseline.json",
        "runs/control.x.baseline.json",
        "runs/migration.m.own-suite.json",
    ]
    for item in files:
        (layout.reports_dir / item).parent.mkdir(parents=True, exist_ok=True)
        (layout.reports_dir / item).write_text("{}", encoding="utf-8")
    removed = invalidate_downstream(layout, "submit-change")
    assert sorted(removed) == [
        "results/migrations/validation.json",
        "results/runs/execute.x.baseline.json",
        "results/verification/results.json",
        "runs/control.x.baseline.json",
        "runs/migration.m.own-suite.json",
    ]
    assert (layout.reports_dir / "results/consumers/r/findings.json").is_file()
    assert invalidate_downstream(layout, "validate-migrations") == []
