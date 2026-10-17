"""Command line for the Shockwave Stripe demo: ``.venv/bin/python demo/scripts/demo.py <command>``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from shockwave_demo.common import (
    ANALYSIS_LABEL,
    DEFAULT_CACHE_DIR,
    DEFAULT_NODE,
    DEFAULT_REPORTS_DIR,
    DEFAULT_WORK_DIR,
    PROVIDER_SOURCE_STATEMENT,
    RUNNER_LABEL,
    SCAN_FIXTURE_LABEL,
    SIMULATED_LABEL,
    DemoError,
    Layout,
    display_path,
)

PIPELINE = ("inventory", "prepare", "scan", "submit-change", "execute", "validate-migrations")
DESCRIPTION = (
    "Shockwave Stripe demo environment. Stripe is the provider; its public OpenAPI contract is pinned and "
    "unmodified. The proposed change is SIMULATED (not announced by Stripe; Stripe is not a customer and has not "
    "endorsed Shockwave). Runs use the LOCAL RUNNER DOUBLE (trusted local fixtures; plain subprocess, not a "
    "sandbox, not Sai's runner); analysis is deterministic rules (no LLM); consumer dependencies come from "
    "Noirit's scanner where it reproduces every expected use site, otherwise from the labeled, line-verified "
    "FIXTURE; Stripe provider source discovery is unavailable."
)


def _layout(args: argparse.Namespace) -> Layout:
    return Layout(
        work_dir=Path(args.work_dir).resolve(),
        reports_dir=Path(args.reports_dir).resolve(),
        cache_dir=Path(args.cache_dir).resolve(),
        node=str(Path(args.node).resolve()) if Path(args.node).exists() else args.node,
        offline=bool(getattr(args, "offline", False)),
    )


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR), help="generated repositories and scratch runs "
                        "(default: demo/.work)")
    common.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="generated reports (default: "
                        "demo/reports)")
    common.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="fetched public sources (default: "
                        "demo/.cache)")
    common.add_argument("--node", default=str(DEFAULT_NODE), help="Node.js >= 22.18 (default: .venv/bin/node)")
    parser = argparse.ArgumentParser(prog="demo.py", description=DESCRIPTION)
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, parents=[common], help=help_text, description=help_text)

    fetch = add("fetch-sources", "verify the pinned public Stripe sources (offline; --network fetches the pinned "
                "commits read-only)")
    fetch.add_argument("--network", action="store_true", help="read-only shallow fetch of each pinned commit from "
                       "public GitHub into <cache-dir>/sources")
    prepare = add("prepare", "build the three consumer repositories as deterministic local git snapshots, install "
                  "stripe-node, run each consumer's own suite")
    prepare.add_argument("--offline", action="store_true", help="install node packages from the npm cache only")
    add("inventory", "discover and measure the pinned Stripe OpenAPI contract (no provider source: unavailable)")
    server = add("fixture-server", "serve SIMULATED Stripe responses on 127.0.0.1 (not Stripe)")
    server.add_argument("--variant", choices=("baseline", "proposed"), default="baseline")
    server.add_argument("--port", type=int, default=8765, help="0 picks a free port (default 8765)")
    server.add_argument("--max-requests", type=int, default=None, help="stop after N requests (scripted use)")
    scan = add("scan", "consumer dependencies: Noirit's scanner if importable, else the labeled fixture adapter")
    scan.add_argument("--use-fixture", action="store_true", help="use the labeled fixture even if a scanner exists")
    add("submit-change", "submit the SIMULATED change: detect_changes + analyze_impact (rules) + views")
    add("execute", "old/proposed scenario runs on the LOCAL RUNNER DOUBLE, not_run results, the reporting control")
    add("validate-migrations", "apply and validate the prepared migration patches for verified regressions")
    reset = add("reset", "delete only generated demo state (demo/.work and generated files under demo/reports)")
    reset.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    run_all = add("all", "inventory -> prepare -> scan -> submit-change -> execute -> validate-migrations (offline)")
    run_all.add_argument("--update-expected", action="store_true", help="replace demo/expected/results with this "
                         "run's normalized results")
    live = add("live-check", "OPTIONAL Stripe TEST-mode check of the consumers (needs explicit authorization)")
    live.add_argument("--authorize-live-test-mode", action="store_true", help="explicitly authorize calls to "
                      "Stripe TEST mode with STRIPE_SECRET_KEY (sk_test_ only)")
    live.add_argument("--payment-intent", help="an existing test-mode PaymentIntent id (pi_...)")
    live.add_argument("--create-test-payment-intent", action="store_true", help="opt in: create a USD 10.00 "
                      "test-mode PaymentIntent confirmed with pm_card_visa")
    return parser


def _stage(name: str) -> Callable[[Layout], list[str]]:
    if name == "inventory":
        from shockwave_demo.inventory import run_inventory

        return run_inventory
    if name == "prepare":
        from shockwave_demo.prepare import run_prepare

        return run_prepare
    if name == "scan":
        from shockwave_demo.scan import run_scan

        return run_scan
    if name == "submit-change":
        from shockwave_demo.change import run_submit_change

        return run_submit_change
    if name == "execute":
        from shockwave_demo.execute import run_execute

        return run_execute
    if name == "validate-migrations":
        from shockwave_demo.migrations import run_validate_migrations

        return run_validate_migrations
    raise ValueError(name)


def _emit(lines: Sequence[str]) -> None:
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _scan_mode() -> str:
    import importlib.util

    if importlib.util.find_spec("scanner") is None:
        return SCAN_FIXTURE_LABEL
    return "Noirit's scanner per repository where it reproduces every expected use site; otherwise the labeled fixture"


def run_all(layout: Layout, *, update_expected: bool) -> int:
    from shockwave_demo.reset import run_reset
    from shockwave_demo.results import compare_with_expected, write_index
    from shockwave_demo.results import update_expected as replace_expected

    _emit(
        [
            "Shockwave Stripe demo - offline run",
            f"  {SIMULATED_LABEL}",
            f"  analysis: {ANALYSIS_LABEL}",
            f"  runner:   {RUNNER_LABEL}",
            f"  scan:     {_scan_mode()}",
            f"  {PROVIDER_SOURCE_STATEMENT}",
            "",
        ]
    )
    _emit(run_reset(layout))
    offline = replace(layout, offline=True)
    for name in PIPELINE:
        _emit(["", f"== {name} =="])
        if name == "prepare":
            try:
                lines = _stage(name)(offline)
            except DemoError as error:
                raise DemoError(
                    f"{error}\n`all` runs offline: if the checkout's node packages are not in the npm cache yet, run "
                    "`.venv/bin/python demo/scripts/demo.py prepare` once with network access, then rerun `all`"
                ) from None
        else:
            lines = _stage(name)(offline)
        write_index(offline)
        _emit(lines)
    comparison = compare_with_expected(offline)
    _emit(["", "== reference results =="])
    if update_expected:
        written = replace_expected(offline)
        _emit([f"  demo/expected/results replaced with this run's normalized results ({written} files)"])
    elif not (comparison["missing"] or comparison["unexpected"] or comparison["different"]):
        _emit([f"  results/ is byte-identical to demo/expected/results ({comparison['identical']} files)"])
    else:
        _emit(
            [
                "  results/ differs from demo/expected/results:",
                *(f"    missing    {item}" for item in comparison["missing"]),
                *(f"    unexpected {item}" for item in comparison["unexpected"]),
                *(f"    different  {item}" for item in comparison["different"]),
            ]
        )
    _emit([f"  reports: {display_path(offline.reports_dir, offline)} (results/ normalized; runs/ with logs)"])
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    layout = _layout(args)
    try:
        if args.command == "fetch-sources":
            from shockwave_demo.sources import run_fetch_sources

            _emit(run_fetch_sources(layout, network=args.network))
        elif args.command == "fixture-server":
            from shockwave_demo.fixture_server import serve

            return serve(args.variant, args.port, max_requests=args.max_requests, out=lambda line: _emit([line]))
        elif args.command == "reset":
            from shockwave_demo.reset import run_reset

            _emit(run_reset(layout, dry_run=args.dry_run))
        elif args.command == "all":
            return run_all(layout, update_expected=args.update_expected)
        elif args.command == "live-check":
            from shockwave_demo.live_check import run_live_check

            _emit(
                run_live_check(
                    layout,
                    authorized=args.authorize_live_test_mode,
                    payment_intent=args.payment_intent,
                    create=args.create_test_payment_intent,
                )
            )
        else:
            from shockwave_demo.results import invalidate_downstream, write_index

            removed = invalidate_downstream(layout, args.command)
            lines = _stage(args.command)(layout)
            if removed:
                lines.append(f"  removed     {len(removed)} downstream result file(s) derived from the previous state")
            _emit(lines)
            write_index(layout)
    except DemoError as error:
        from shockwave_demo.live_check import LiveCheckRefused

        print(f"demo.py {args.command}: {'REFUSED' if isinstance(error, LiveCheckRefused) else 'FAILED'}: {error}",
              file=sys.stderr)
        return 2 if isinstance(error, LiveCheckRefused) else 1
    return 0
