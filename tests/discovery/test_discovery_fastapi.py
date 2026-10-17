"""Static FastAPI route extraction: the demo provider, edge fixtures and bounded file walking.

Line numbers are computed from the fixture files, never hardcoded, so the assertions
stay tied to the source text the scanner reports as evidence.
"""

from __future__ import annotations

import ast
import os
import textwrap
import time
from pathlib import Path

import pytest

from discovery import _source_files as source_files
from discovery import source_fastapi
from discovery.records import DiscoveryError, SourceScan
from discovery.source_fastapi import scan_fastapi_routes

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SERVICE = REPO_ROOT / "examples" / "provider" / "demo_payments" / "service"
DEMO_PACKAGE = DEMO_SERVICE / "demo_payments"
EDGE_SERVICE = Path(__file__).resolve().parent / "fixtures" / "fastapi" / "edge_service"


def line_of(path: Path, needle: str) -> int:
    matches = [
        number
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if needle in line
    ]
    assert len(matches) == 1, f"{needle!r} must appear exactly once in {path.name}"
    return matches[0]


def write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def route_keys(scan: SourceScan) -> list[tuple[str, str]]:
    return [(route.method, route.path) for route in scan.routes]


def can_symlink(tmp_path: Path) -> bool:
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


# Demo provider -------------------------------------------------------------------


def test_demo_provider_emits_exactly_the_literal_routes() -> None:
    scan = scan_fastapi_routes(DEMO_SERVICE)

    charges = DEMO_PACKAGE / "charges.py"
    health = DEMO_PACKAGE / "health.py"
    assert [
        (route.method, route.path, route.file, route.line_start, route.handler) for route in scan.routes
    ] == [
        ("GET", "/healthz", "demo_payments/health.py", line_of(health, '@router.get("/healthz")'), "healthz"),
        (
            "GET",
            "/v1/charges/{id}",
            "demo_payments/charges.py",
            line_of(charges, '@router.get("/{id}")'),
            "retrieve_charge",
        ),
        (
            "GET",
            "/v1/charges/{id}/receipt",
            "demo_payments/charges.py",
            line_of(charges, '@router.get("/{id}/receipt")'),
            "retrieve_charge_receipt",
        ),
    ]
    charge = scan.routes[1]
    assert charge.framework == "fastapi"
    assert charge.raw_path == "/v1/charges/{id}"
    assert charge.param_mapping == (("id", "{id}"),)
    assert charge.line_end == charge.line_start
    assert charge.excerpt == '@router.get("/{id}")'
    assert charge.include_in_schema is True
    assert all(route.confidence == "direct" for route in scan.routes)


def test_demo_refunds_route_behind_dynamic_prefix_is_reported_not_guessed() -> None:
    scan = scan_fastapi_routes(DEMO_SERVICE)

    prefix_line = line_of(DEMO_PACKAGE / "refunds.py", "APIRouter(prefix=settings.REFUNDS_PREFIX")
    assert scan.unsupported_patterns == [f"fastapi:dynamic-prefix demo_payments/refunds.py:{prefix_line}"]
    assert not any("refund" in route.path for route in scan.routes)
    assert len(scan.warnings) == 1
    assert "1 route registration(s) could not be resolved" in scan.warnings[0]
    assert "dynamic prefix: 1" in scan.warnings[0]


def test_demo_provider_coverage_counts_every_python_file() -> None:
    scan = scan_fastapi_routes(DEMO_SERVICE)

    python_files = [path for path in DEMO_SERVICE.rglob("*.py") if "__pycache__" not in path.parts]
    assert scan.files_scanned == len(python_files)
    assert scan.files_skipped == 0


# Edge-case fixture tree -------------------------------------------------------------

EDGE_ROUTES = [
    ("GET", "/admin/health", "shop/admin.py", '@admin_app.get("/admin/health")', "admin_health"),
    ("POST", "/api/v1/accounts/users", "shop/routers/users.py", '@router.post("")', "create_user"),
    ("GET", "/api/v1/accounts/users/{user_id}", "shop/routers/users.py", '@router.get("/{user_id}")', "get_user"),
    ("GET", "/api/v1/ping", "shop/routers/v1.py", '@router.get("/ping")', "ping"),
    ("GET", "/files/{file_path}", "shop/routers/files.py", '@router.get("/{file_path:path}")', "download"),
    ("GET", "/internal/metrics", "shop/main.py", '@app.get("/internal/metrics"', "metrics"),
    ("POST", "/reports/export", "shop/routers/reports.py", '@router.api_route("/export"', "export_report"),
    ("PUT", "/reports/export", "shop/routers/reports.py", '@router.api_route("/export"', "export_report"),
    ("GET", "/reports/summary", "shop/routers/reports.py", '@router.api_route("/summary")', "summary"),
    ("GET", "/status", "shop/main.py", 'app.add_api_route("/status"', "get_status"),
    ("HEAD", "/status", "shop/main.py", 'app.add_api_route("/status"', "get_status"),
]


def test_edge_fixture_emits_exactly_the_resolvable_routes() -> None:
    scan = scan_fastapi_routes(EDGE_SERVICE)

    expected = [
        (method, path, file, line_of(EDGE_SERVICE / file, needle), handler)
        for method, path, file, needle, handler in EDGE_ROUTES
    ]
    assert [
        (route.method, route.path, route.file, route.line_start, route.handler) for route in scan.routes
    ] == expected
    assert all(route.framework == "fastapi" and route.confidence == "direct" for route in scan.routes)


def test_edge_fixture_reports_every_unresolved_pattern_with_its_location() -> None:
    scan = scan_fastapi_routes(EDGE_SERVICE)

    main = EDGE_SERVICE / "shop" / "main.py"
    orphan = EDGE_SERVICE / "shop" / "routers" / "orphan.py"
    expected = [
        ("unresolved-router-include", line_of(main, "app.include_router(auth_router")),
        ("dynamic-path", line_of(main, "@app.get(HEALTH_PATH)")),
        ("websocket", line_of(main, '@app.websocket("/ws/updates")')),
        ("mount", line_of(main, 'app.mount("/static"')),
        ("mount", line_of(main, 'app.mount("/legacy"')),
        ("route-after-include", line_of(main, '@reports_router.get("/late")')),
    ]
    assert scan.unsupported_patterns == [
        *(f"fastapi:{kind} shop/main.py:{line}" for kind, line in sorted(expected, key=lambda item: item[1])),
        f"fastapi:router-not-mounted shop/routers/orphan.py:{line_of(orphan, 'router = APIRouter(')}",
    ]


def test_edge_fixture_prefixes_converters_and_schema_flags() -> None:
    routes = {(route.method, route.path): route for route in scan_fastapi_routes(EDGE_SERVICE).routes}

    nested = routes[("GET", "/api/v1/accounts/users/{user_id}")]
    assert nested.raw_path == "/api/v1/accounts/users/{user_id}"  # app prefix + v1 + include + users
    assert nested.param_mapping == (("user_id", "{user_id}"),)

    converted = routes[("GET", "/files/{file_path}")]
    assert converted.raw_path == "/files/{file_path:path}"
    assert converted.param_mapping == (("file_path", "{file_path:path}"),)

    assert routes[("POST", "/api/v1/accounts/users")].raw_path == "/api/v1/accounts/users"
    assert routes[("GET", "/reports/summary")].raw_path == "/reports/summary"  # module-constant prefix
    assert routes[("GET", "/internal/metrics")].include_in_schema is False
    assert routes[("GET", "/status")].include_in_schema is True
    assert routes[("GET", "/status")].excerpt == (
        'app.add_api_route("/status", get_status, methods=["GET", "HEAD"])'
    )


def test_edge_fixture_ignores_docstrings_comments_and_mounted_sub_applications() -> None:
    paths = {route.path for route in scan_fastapi_routes(EDGE_SERVICE).routes}

    assert not any("fake" in path for path in paths)
    assert not {"/old", "/legacy/old", "/orphan/ping", "/reports/late", "/healthz"} & paths
    assert not any(path.startswith(("/auth", "/ws", "/static")) for path in paths)


def test_edge_fixture_skips_the_syntax_error_file_and_says_why() -> None:
    scan = scan_fastapi_routes(EDGE_SERVICE)

    broken = EDGE_SERVICE / "shop" / "routers" / "broken.py"
    python_files = list(EDGE_SERVICE.rglob("*.py"))
    assert scan.files_skipped == 1
    assert scan.files_scanned == len(python_files) - 1
    assert scan.warnings[0] == f"shop/routers/broken.py: syntax error at line {line_of(broken, 'def broken(:')}"
    assert scan.warnings[1].startswith("fastapi: 4 route registration(s) could not be resolved")
    for reason in (
        "dynamic path: 1",
        "mounted application: 1",
        "registered after include_router: 1",
        "router not mounted: 1",
    ):
        assert reason in scan.warnings[1]


def test_scans_are_deterministic() -> None:
    assert scan_fastapi_routes(EDGE_SERVICE) == scan_fastapi_routes(EDGE_SERVICE)
    assert scan_fastapi_routes(DEMO_SERVICE) == scan_fastapi_routes(DEMO_SERVICE)


# Literal and resolution rules -----------------------------------------------------------


def test_literal_rules_for_prefixes_and_paths(tmp_path: Path) -> None:
    write(
        tmp_path,
        "app.py",
        """
        from fastapi import APIRouter, FastAPI

        BASE = "/base"
        TWICE = "/first"
        TWICE = "/second"
        VERSION = "v1"

        app = FastAPI()
        literal = APIRouter(prefix=BASE + f"/joined")
        rebound = APIRouter(prefix=TWICE)


        @literal.get("/items")
        def items(): ...


        @rebound.get("/never")
        def never(): ...


        @app.get(f"/{VERSION}/info")
        def info(): ...


        def register(BASE):
            @app.get(BASE)
            def shadowed(): ...


        app.include_router(literal)
        app.include_router(rebound)
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert route_keys(scan) == [("GET", "/base/joined/items")]
    source = tmp_path / "app.py"
    assert scan.unsupported_patterns == [
        f"fastapi:dynamic-prefix app.py:{line_of(source, 'APIRouter(prefix=TWICE)')}",
        f"fastapi:dynamic-path app.py:{line_of(source, '@app.get(f')}",
        f"fastapi:dynamic-path app.py:{line_of(source, '@app.get(BASE)')}",
    ]


def test_app_factory_applied_decorators_and_dynamic_methods(tmp_path: Path) -> None:
    write(
        tmp_path,
        "factory.py",
        """
        from fastapi import APIRouter, FastAPI

        METHODS = compute_methods()
        router = APIRouter()


        def list_orders(): ...


        router.get("/orders")(list_orders)


        @router.api_route("/bulk", methods=METHODS)
        def bulk(): ...


        def create_app() -> FastAPI:
            app = FastAPI()
            app.include_router(router, prefix="/shop")

            @app.post("/checkout")
            def checkout(): ...

            return app
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert [(route.method, route.path, route.handler) for route in scan.routes] == [
        ("POST", "/checkout", "checkout"),
        ("GET", "/shop/orders", "list_orders"),
    ]
    assert scan.unsupported_patterns == [
        f"fastapi:dynamic-methods factory.py:{line_of(tmp_path / 'factory.py', 'methods=METHODS')}"
    ]


def test_cross_module_import_forms_resolve_inside_the_source_root(tmp_path: Path) -> None:
    write(tmp_path, "svc/__init__.py", "")
    write(tmp_path, "svc/api/__init__.py", "")
    write(
        tmp_path,
        "svc/api/orders.py",
        """
        from fastapi import APIRouter

        router = APIRouter(prefix="/orders")


        @router.get("/{order_id:int}")
        def get_order(order_id: int): ...
        """,
    )
    write(
        tmp_path,
        "svc/api/refunds.py",
        """
        import fastapi

        router = fastapi.APIRouter(prefix="/refunds")


        @router.get("")
        def list_refunds(): ...
        """,
    )
    write(
        tmp_path,
        "svc/api/routes.py",
        """
        from fastapi import APIRouter

        from . import orders
        from .refunds import router as refunds_router

        api = APIRouter(prefix="/api")
        api.include_router(orders.router)
        api.include_router(refunds_router)
        """,
    )
    write(
        tmp_path,
        "svc/main.py",
        """
        import svc.api.routes as routes
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(routes.api, prefix="/v2")
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert [(route.method, route.path, route.raw_path, route.file) for route in scan.routes] == [
        ("GET", "/v2/api/orders/{order_id}", "/v2/api/orders/{order_id:int}", "svc/api/orders.py"),
        ("GET", "/v2/api/refunds", "/v2/api/refunds", "svc/api/refunds.py"),
    ]
    assert scan.routes[0].param_mapping == (("order_id", "{order_id:int}"),)
    assert scan.unsupported_patterns == []
    assert scan.warnings == []


def test_unresolvable_receivers_and_includes_are_reported(tmp_path: Path) -> None:
    source = write(
        tmp_path,
        "plugins.py",
        """
        from fastapi import FastAPI
        from somewhere_else import external_router

        app = FastAPI()
        app.include_router(external_router)
        app.include_router(make_router(), prefix="/made")


        def register(target):
            @target.get("/plugin")
            def plugin(): ...
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert scan.routes == []
    assert scan.unsupported_patterns == [
        f"fastapi:unresolved-router-include plugins.py:{line_of(source, 'app.include_router(external_router)')}",
        f"fastapi:unresolved-router-include plugins.py:{line_of(source, 'make_router()')}",
        f"fastapi:unresolved-router plugins.py:{line_of(source, '@target.get')}",
    ]


def test_registrations_on_receivers_outside_the_tree_are_reported(tmp_path: Path) -> None:
    write(tmp_path, "svc/__init__.py", "")
    write(tmp_path, "svc/routing.py", "def get(path):\n    return lambda function: function\n")
    source = write(
        tmp_path,
        "svc/main.py",
        """
        import platform_kit
        from fastapi_utils.inferring_router import InferringRouter

        from svc import routing

        app = platform_kit.create_app(name="payments")
        router = InferringRouter(prefix="/v1/charges")


        @app.get("/v1/health")
        def health(): ...


        @router.post("")
        def create_charge(): ...


        @routing.get("/v1/legacy")
        def legacy(): ...
        """,
    )
    write(
        tmp_path,
        "tests/test_main.py",
        """
        from unittest import mock

        from fastapi.testclient import TestClient


        @mock.patch("svc.main.health")
        def test_health(fake): ...
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    # Another library's app or router and an in-tree module: not proof of absence, a gap.
    assert scan.routes == []
    assert scan.unsupported_patterns == [
        f"fastapi:unresolved-router svc/main.py:{line_of(source, needle)}"
        for needle in ('@app.get("/v1/health")', '@router.post("")', '@routing.get(')
    ]
    assert "(unresolved router: 3)" in scan.warnings[0]  # mock.patch("svc.main...") is no route


def test_src_layout_imports_resolve_from_the_repository_root(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[project]\nname = 'payments'\n")
    write(tmp_path, "src/payments/__init__.py", "")
    write(tmp_path, "src/payments/routes/__init__.py", "")
    write(
        tmp_path,
        "src/payments/app.py",
        """
        from fastapi import FastAPI

        from payments.routes import refunds

        app = FastAPI()
        app.include_router(refunds.router)
        """,
    )
    write(
        tmp_path,
        "src/payments/routes/charges.py",
        """
        from payments.app import app


        @app.get("/v1/charges/{id}")
        def retrieve_charge(id: str): ...
        """,
    )
    write(
        tmp_path,
        "src/payments/routes/refunds.py",
        """
        from fastapi import APIRouter

        router = APIRouter(prefix="/v1/refunds")


        @router.get("/{id}")
        def retrieve_refund(id: str): ...
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert [(route.method, route.path, route.file) for route in scan.routes] == [
        ("GET", "/v1/charges/{id}", "src/payments/routes/charges.py"),
        ("GET", "/v1/refunds/{id}", "src/payments/routes/refunds.py"),
    ]
    assert scan.unsupported_patterns == []
    assert scan.warnings == []


def test_ambiguous_src_layouts_are_reported_not_guessed(tmp_path: Path) -> None:
    routes: dict[str, Path] = {}
    for service in ("billing", "ledger"):  # two projects whose src/ both hold a package "app"
        write(tmp_path, f"{service}/src/app/__init__.py", "")
        write(tmp_path, f"{service}/src/app/main.py", "from fastapi import FastAPI\n\napp = FastAPI()\n")
        routes[service] = write(
            tmp_path,
            f"{service}/src/app/routes.py",
            f'from app.main import app\n\n\n@app.get("/v1/{service}")\ndef handler(): ...\n',
        )
    scan = scan_fastapi_routes(tmp_path)

    assert scan.routes == []
    assert scan.unsupported_patterns == [
        f"fastapi:unresolved-router {service}/src/app/routes.py:{line_of(path, '@app.get')}"
        for service, path in routes.items()
    ]


def test_include_cycles_and_depth_limit_are_bounded(tmp_path: Path) -> None:
    lines = ["from fastapi import APIRouter, FastAPI", "", "app = FastAPI()"]
    lines += [f'r{index} = APIRouter(prefix="/r{index}")' for index in range(10)]
    lines += ['r9.get("/deep")(print)', 'r2.get("/mid")(print)', "r1.include_router(r0)"]
    # Deepest first, as FastAPI requires: include_router copies the routes present when it runs.
    lines += [f"r{index}.include_router(r{index + 1})" for index in reversed(range(9))]
    lines += ["app.include_router(r0)"]
    write(tmp_path, "deep.py", "\n".join(lines) + "\n")
    scan = scan_fastapi_routes(tmp_path)

    source = tmp_path / "deep.py"
    assert route_keys(scan) == [("GET", "/r0/r1/r2/mid")]
    assert scan.unsupported_patterns == [
        f"fastapi:include-cycle deep.py:{line_of(source, 'r1.include_router(r0)')}",
        f"fastapi:include-depth-limit deep.py:{line_of(source, 'r7.include_router(r8)')}",
    ]
    assert "include depth limit: 1" in scan.warnings[0]


def test_include_after_the_parent_was_included_serves_nothing(tmp_path: Path) -> None:
    source = write(
        tmp_path,
        "late.py",
        """
        from fastapi import APIRouter, FastAPI

        app = FastAPI()
        parent = APIRouter(prefix="/parent")
        child = APIRouter(prefix="/child")


        @child.get("/x")
        def x(): ...


        app.include_router(parent)
        parent.include_router(child)
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert scan.routes == []
    assert scan.unsupported_patterns == [
        f"fastapi:late-include late.py:{line_of(source, 'parent.include_router(child)')}"
    ]
    assert "included after its parent was included: 1" in scan.warnings[0]


def test_include_in_schema_and_registration_order_follow_fastapi(tmp_path: Path) -> None:
    source = write(
        tmp_path,
        "order.py",
        """
        from fastapi import APIRouter, FastAPI

        app = FastAPI()
        hidden = APIRouter(prefix="/hidden")
        early = APIRouter(prefix="/early")


        @hidden.get("/a")
        def hidden_a(): ...


        @early.get("/before")
        def before(): ...


        app.include_router(hidden, include_in_schema=False)
        app.include_router(early)


        @early.get("/after")
        def after(): ...
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert [(route.path, route.include_in_schema) for route in scan.routes] == [
        ("/early/before", True),
        ("/hidden/a", False),
    ]
    after_line = line_of(source, '@early.get("/after")')
    assert scan.unsupported_patterns == [f"fastapi:route-after-include order.py:{after_line}"]


def test_other_unsupported_registrations(tmp_path: Path) -> None:
    source = write(
        tmp_path,
        "misc.py",
        """
        from fastapi import FastAPI
        from starlette.routing import Route

        app = FastAPI(routes=[Route("/raw", endpoint=print)])
        app.add_route("/starlette", print)
        app.add_api_websocket_route("/socket", print)
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert scan.routes == []
    assert scan.unsupported_patterns == [
        f"fastapi:starlette-route misc.py:{line_of(source, 'app = FastAPI(routes=')}",
        f"fastapi:starlette-route misc.py:{line_of(source, 'app.add_route(')}",
        f"fastapi:websocket misc.py:{line_of(source, 'app.add_api_websocket_route(')}",
    ]


def test_scanned_code_is_parsed_never_executed(tmp_path: Path) -> None:
    write(
        tmp_path,
        "side_effects.py",
        """
        import pathlib

        pathlib.Path(__file__).with_name("EXECUTED").write_text("ran")
        raise SystemExit("scanned code must never run")

        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/still-found")
        def still_found(): ...
        """,
    )
    scan = scan_fastapi_routes(tmp_path)

    assert route_keys(scan) == [("GET", "/still-found")]
    assert not (tmp_path / "EXECUTED").exists()


# Bounded resolution and excerpts ---------------------------------------------------------


@pytest.mark.parametrize("order", ["late", "deepest-first"])
def test_include_chains_longer_than_the_recursion_limit_are_reported(tmp_path: Path, order: str) -> None:
    count = 1200  # deeper than Python's default recursion limit of 1000
    lines = ["from fastapi import APIRouter, FastAPI", "", "app = FastAPI()"]
    lines += [f'r{index} = APIRouter(prefix="/r{index}")' for index in range(count)]
    lines += [f'r{count - 1}.get("/leaf")(print)']
    includes = [f"r{index}.include_router(r{index + 1})" for index in range(count - 1)]
    if order == "late":  # the app copies r0 before anything is included into r0
        lines += ["app.include_router(r0)", *includes]
    else:  # wired deepest first, as FastAPI requires; the depth limit stops the walk
        lines += [*reversed(includes), "app.include_router(r0)"]
    source = write(tmp_path, "chain.py", "\n".join(lines) + "\n")
    write(tmp_path, "ok.py", 'from fastapi import FastAPI\n\nother = FastAPI()\nother.get("/v1/fine")(print)\n')
    scan = scan_fastapi_routes(tmp_path)

    assert route_keys(scan) == [("GET", "/v1/fine")]  # an unrelated file keeps its routes
    kind, needle = ("late-include", "r0.include_router(r1)")
    if order != "late":
        kind, needle = ("include-depth-limit", "r7.include_router(r8)")
    assert scan.unsupported_patterns == [f"fastapi:{kind} chain.py:{line_of(source, needle)}"]


def test_include_graph_budget_stops_the_walk_and_reports_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = ["from fastapi import APIRouter, FastAPI", "", "app = FastAPI()", "router = APIRouter()"]
    lines += ["other = APIRouter()", 'router.get("/a")(print)', 'router.get("/b")(print)']
    lines += ['other.get("/c")(print)']
    lines += [f'app.include_router(router, prefix="/p{index}")' for index in range(10)]
    lines += ['app.include_router(other, prefix="/z")']
    source = write(tmp_path, "many.py", "\n".join(lines) + "\n")
    # Entering the app costs 1 + 11 includes; each entry into ``router`` 1 + 2 registrations.
    monkeypatch.setattr(source_fastapi, "_MAX_GRAPH_WORK", 18)
    scan = scan_fastapi_routes(tmp_path)

    assert route_keys(scan) == [("GET", "/p0/a"), ("GET", "/p0/b"), ("GET", "/p1/a"), ("GET", "/p1/b")]
    stopped = line_of(source, '"/p2"')  # the include the walk could no longer afford
    assert scan.unsupported_patterns == [f"fastapi:include-graph-limit many.py:{stopped}"]
    assert scan.warnings[0].startswith("fastapi: 1 route registration(s) could not be resolved")
    assert "(include graph limit: 1)" in scan.warnings[0]  # /z/c: never reached, not "unmounted"
    assert scan.warnings[1] == "fastapi: include graph too large; scan truncated during route resolution"


def test_route_resolution_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "app.py", 'from fastapi import FastAPI\n\napp = FastAPI()\napp.get("/v1/x")(print)\n')

    def overflow(self: object) -> list:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(source_fastapi._Resolver, "resolve_routes", overflow)
    scan = scan_fastapi_routes(tmp_path)

    assert scan.routes == []
    assert scan.unsupported_patterns == ["fastapi:route-resolution-failed"]
    (warning,) = scan.warnings
    assert warning.startswith("fastapi: route resolution exceeded the recursion limit;")
    assert warning.endswith("(scan truncated)")


def test_excerpts_follow_python_line_ends_and_byte_offsets(tmp_path: Path) -> None:
    accent, form_feed = chr(0xE9), chr(0x0C)
    text = (
        "from fastapi import FastAPI\r\n"
        f"app = FastAPI(title='caf{accent}')  # a form feed{form_feed} does not end a line\r"
        f"@app.get('/caf{accent}/{{id}}',\n"
        "         include_in_schema=False)\r\n"
        "def cafe(id): ...\n"
        f"app.get('/x{accent}')(cafe); app.get('/y')(cafe)\n"
    )
    (tmp_path / "app.py").write_bytes(text.encode("utf-8"))
    scan = scan_fastapi_routes(tmp_path)

    expected = {
        f"/caf{accent}/{{id}}": (3, f"@app.get('/caf{accent}/{{id}}', include_in_schema=False)"),
        f"/x{accent}": (6, f"app.get('/x{accent}')(cafe)"),
        "/y": (6, "app.get('/y')(cafe)"),
    }
    assert {route.path: (route.line_start, route.excerpt) for route in scan.routes} == expected
    calls = [node for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Call) and node.args]
    segments = {source_files.excerpt_text(ast.get_source_segment(text, node) or "") for node in calls}
    assert {excerpt.removeprefix("@") for _, excerpt in expected.values()} <= segments


def test_excerpts_read_a_bounded_window_of_long_registrations(tmp_path: Path) -> None:
    blank_lines = "\n" * (2 * source_files.EXCERPT_WINDOW)
    write(tmp_path, "app.py", f'from fastapi import FastAPI\n\napp = FastAPI()\napp.get("/big",{blank_lines})(print)\n')
    (route,) = scan_fastapi_routes(tmp_path).routes

    assert route.excerpt == 'app.get("/big", ...'  # cut at the window and marked, not silently
    assert route.line_end - route.line_start == 2 * source_files.EXCERPT_WINDOW


def test_many_routes_scan_in_linear_time(tmp_path: Path) -> None:
    count = 6000
    write(
        tmp_path,
        "app.py",
        "from fastapi import FastAPI\napp = FastAPI()\n"
        + "".join(f"@app.get('/r{index}')\ndef r{index}(): ...\n" for index in range(count)),
    )
    started = time.perf_counter()
    scan = scan_fastapi_routes(tmp_path)

    assert len(scan.routes) == count
    assert time.perf_counter() - started < 3.0  # ~12 s when every excerpt re-split the file


# Bounded, symlink-safe walking -----------------------------------------------------------


def test_source_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    not_a_directory = write(tmp_path, "file.py", "x = 1\n")
    for candidate in (not_a_directory, tmp_path / "missing"):
        with pytest.raises(DiscoveryError) as error:
            scan_fastapi_routes(candidate)
        assert str(tmp_path) not in str(error.value)  # internal paths are not echoed
    with pytest.raises(DiscoveryError):
        scan_fastapi_routes(tmp_path, max_files=0)


def test_symlinks_outside_the_root_are_never_followed(tmp_path: Path) -> None:
    if not can_symlink(tmp_path):
        pytest.skip("symlinks are not supported here")
    outside = tmp_path / "outside"
    write(
        outside,
        "secret_app.py",
        """
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/outside-secret")
        def secret(): ...
        """,
    )
    root = tmp_path / "root"
    write(
        root,
        "app.py",
        """
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/inside")
        def inside(): ...
        """,
    )
    (root / "linked_dir").symlink_to(outside, target_is_directory=True)
    (root / "linked_app.py").symlink_to(outside / "secret_app.py")
    scan = scan_fastapi_routes(root)

    assert route_keys(scan) == [("GET", "/inside")]
    assert scan.files_scanned == 1
    assert scan.files_skipped == 1  # the symlinked .py file
    assert "linked_app.py: skipped (symbolic link not followed)" in scan.warnings
    assert "linked_dir: skipped (symlinked directory not followed)" in scan.warnings


def test_skipped_directories_special_files_and_unreadable_content(tmp_path: Path) -> None:
    app = """
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/{name}")
    def handler(): ...
    """
    write(tmp_path, "real.py", app.replace("{name}", "real"))
    for directory in ("node_modules/pkg", ".venv/lib", "build", "__pycache__", "site-packages", ".git"):
        write(tmp_path, f"{directory}/hidden.py", app.replace("{name}", "hidden"))
    (tmp_path / "latin1.py").write_bytes(b"NAME = '\xe9'\n")
    write(tmp_path, "huge.py", "x = 1\n" * 50)
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "pipe.py")  # must never be opened (a read would block)
    scan = scan_fastapi_routes(tmp_path, max_file_bytes=200)

    assert route_keys(scan) == [("GET", "/real")]
    assert scan.files_scanned == 1
    assert scan.files_skipped == 2
    assert "huge.py: skipped (larger than 200 bytes)" in scan.warnings
    assert "latin1.py: skipped (not valid UTF-8)" in scan.warnings


def test_max_files_truncates_with_a_warning(tmp_path: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        write(tmp_path, name, "x = 1\n")
    scan = scan_fastapi_routes(tmp_path, max_files=2)

    assert scan.files_scanned == 2
    assert scan.files_skipped == 1
    assert scan.warnings[0] == "scan truncated: 1 file(s) beyond max_files=2 were not scanned"


def test_entry_and_total_size_budgets_truncate_with_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(4):
        write(tmp_path, f"wide/m{index}.py", "x = 1\n")
    write(tmp_path, "a.py", "x = 1\n")
    monkeypatch.setattr(source_files, "_MIN_ENTRY_BUDGET", 3)
    monkeypatch.setattr(source_files, "_ENTRIES_PER_FILE", 1)
    scan = scan_fastapi_routes(tmp_path, max_files=1)

    assert (scan.files_scanned, scan.files_skipped) == (1, 0)  # "wide" holds more than the budget
    assert scan.warnings == [
        "scan truncated: directory walk stopped after 3 entries; files beyond that point were not counted"
    ]

    monkeypatch.setattr(source_files, "_MIN_ENTRY_BUDGET", 100)
    monkeypatch.setattr(source_files, "_MAX_TOTAL_BYTES", 13)  # room for two 6-byte files
    scan = scan_fastapi_routes(tmp_path)

    assert (scan.files_scanned, scan.files_skipped) == (2, 3)
    assert scan.warnings == [
        "scan truncated: total size limit of 13 bytes reached; 3 file(s) were not scanned"
    ]
