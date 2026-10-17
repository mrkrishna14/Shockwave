"""Static Express route extraction: the payments fixture (JS + TS) and lexer/mount edge cases.

Line numbers are computed from the fixture text, never hardcoded.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

from discovery import _source_files as source_files
from discovery import source_express
from discovery.records import DiscoveryError, SourceScan
from discovery.source_express import scan_express_routes

PAYMENTS_APP = Path(__file__).resolve().parent / "fixtures" / "express" / "payments_app"
SERVER_JS = PAYMENTS_APP / "server.js"
SERVER_TS = PAYMENTS_APP / "src" / "server.ts"
REFUNDS_JS = PAYMENTS_APP / "routes" / "refunds.js"


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


# Payments fixture -------------------------------------------------------------------

PAYMENTS_ROUTES = [
    ("GET", "/healthz", "/healthz", "server.js", SERVER_JS, "app.get('/healthz'", None),
    ("GET", "/v1/balance", "/v1/balance", "server.js", SERVER_JS, "app.get(`/v1/balance`", "getBalance"),
    ("GET", "/v1/charges/{id}", "/v1/charges/:id", "server.js", SERVER_JS, "charges.get('/:id',", "getCharge"),
    ("GET", "/v1/charges/{id}/captures", "/v1/charges/:id/captures", "server.js", SERVER_JS, "charges", "listCaptures"),
    ("POST", "/v1/charges/{id}/captures", "/v1/charges/:id/captures", "server.js", SERVER_JS, "charges", "createCapture"),
    ("GET", "/v1/charges/{id}/receipt", "/v1/charges/:id/receipt", "server.js", SERVER_JS, "'/:id/receipt'", "getReceipt"),
    ("POST", "/v1/payments", "/v1/payments", "server.js", SERVER_JS, "app.post('/v1/payments'", "createPayment"),
    ("GET", "/v2/charges/{id}", "/v2/charges/:id", "src/server.ts", SERVER_TS, "charges.get('/:id'", None),
    ("POST", "/v2/charges/{id}/refunds", "/v2/charges/:id/refunds", "src/server.ts", SERVER_TS, ".post(createRefund)", "createRefund"),
    ("GET", "/v2/healthz", "/v2/healthz", "src/server.ts", SERVER_TS, "'/v2/healthz'", None),
]


def chain_start_line() -> int:
    """The ``charges`` line that starts the multi-line route() chain in server.js."""
    lines = SERVER_JS.read_text(encoding="utf-8").splitlines()
    return lines.index("charges") + 1


def test_payments_fixture_emits_exactly_the_literal_routes() -> None:
    scan = scan_express_routes(PAYMENTS_APP)

    expected = []
    for method, path, raw_path, file, source, needle, handler in PAYMENTS_ROUTES:
        line = chain_start_line() if needle == "charges" else line_of(source, needle)
        expected.append((method, path, raw_path, file, line, handler))
    assert [
        (route.method, route.path, route.raw_path, route.file, route.line_start, route.handler)
        for route in scan.routes
    ] == expected
    assert all(
        route.framework == "express" and route.confidence == "direct" and route.include_in_schema is None
        for route in scan.routes
    )


def test_payments_fixture_parameters_excerpts_and_line_spans() -> None:
    routes = {(route.method, route.path, route.file): route for route in scan_express_routes(PAYMENTS_APP).routes}

    charge = routes[("GET", "/v1/charges/{id}", "server.js")]
    assert charge.param_mapping == (("id", ":id"),)
    assert charge.excerpt == "charges.get('/:id', getCharge)"
    assert charge.line_end == charge.line_start

    capture = routes[("POST", "/v1/charges/{id}/captures", "server.js")]
    assert capture.line_end == line_of(SERVER_JS, ".post(createCapture);")
    assert capture.excerpt == (
        "charges .route('/:id/captures') .get(listCaptures) .post(createCapture)"
    )

    typed = routes[("GET", "/v2/charges/{id}", "src/server.ts")]
    assert typed.line_end == typed.line_start + 2  # the inline handler spans three lines
    assert typed.excerpt.startswith("charges.get('/:id', (req: Request<ChargeParams>, res: Response) =>")
    assert all(len(route.excerpt) <= 300 for route in routes.values())


def test_payments_fixture_reports_every_unresolved_pattern() -> None:
    scan = scan_express_routes(PAYMENTS_APP)

    expected = [
        ("routes/refunds.js", line_of(REFUNDS_JS, "const router = express.Router()"), "router-not-mounted"),
        ("server.js", line_of(SERVER_JS, "app.use('/v1/refunds', refundsRouter)"), "cross-file-router"),
        ("server.js", line_of(SERVER_JS, "app.get(reportPath"), "dynamic-path"),
        ("server.js", line_of(SERVER_JS, "app.get(`/v1/exports/"), "dynamic-path"),
        ("server.js", line_of(SERVER_JS, "app.all('/v1/legacy'"), "all-methods"),
        ("server.js", line_of(SERVER_JS, "'/v1/files/*'"), "unsupported-path-syntax"),
        ("server.js", line_of(SERVER_JS, "'/v1/items/:id?'"), "unsupported-path-syntax"),
        ("server.js", line_of(SERVER_JS, "'/v1/disputes/:id("), "unsupported-path-syntax"),
        ("src/server.ts", line_of(SERVER_TS, ".all(methodNotAllowed)"), "all-methods"),
        ("src/server.ts", line_of(SERVER_TS, "app.use('/v2/webhooks', webhooksRouter)"), "cross-file-router"),
        ("src/server.ts", line_of(SERVER_TS, "adminRouter.get("), "unresolved-router"),
    ]
    assert scan.unsupported_patterns == [f"express:{kind} {file}:{line}" for file, line, kind in expected]


def test_payments_fixture_coverage_and_warnings() -> None:
    scan = scan_express_routes(PAYMENTS_APP)

    assert scan.files_scanned == 3  # server.js, routes/refunds.js, src/server.ts (env.d.ts excluded)
    assert scan.files_skipped == 0
    assert len(scan.warnings) == 2
    assert scan.warnings[0].startswith("express: 9 route registration(s) could not be resolved")
    for reason in (
        "all methods: 2",
        "dynamic path: 2",
        "router not mounted: 1",
        "unresolved router: 1",
        "unsupported path syntax: 3",
    ):
        assert reason in scan.warnings[0]
    assert scan.warnings[1].startswith("express: 2 mount(s) use routers from other files")


def test_comments_strings_and_templates_never_produce_routes() -> None:
    paths = {route.path for route in scan_express_routes(PAYMENTS_APP).routes}

    assert not any("fake" in path or "commented" in path for path in paths)
    assert "/v1/refunds/{id}" not in paths  # cross-file router: reported, not guessed
    assert "/v2/admin/audit" not in paths  # unresolved receiver: reported, not guessed


def test_scan_is_deterministic() -> None:
    assert scan_express_routes(PAYMENTS_APP) == scan_express_routes(PAYMENTS_APP)


# Mount composition and classification -------------------------------------------------


def test_nested_in_file_mounts_compose_prefixes(tmp_path: Path) -> None:
    write(
        tmp_path,
        "app.mjs",
        """
        import express, { Router as makeRouter } from 'express';
        import * as ex from 'express';

        const app = express();
        const api = makeRouter();
        const v1 = ex.Router();

        v1.get('/items/:itemId', (req, res) => res.json({}));
        api.use('/v1', v1);
        app.use('/api', api);
        app.use(api);
        """,
    )
    scan = scan_express_routes(tmp_path)

    assert [(route.path, route.raw_path) for route in scan.routes] == [
        ("/api/v1/items/{itemId}", "/api/v1/items/:itemId"),
        ("/v1/items/{itemId}", "/v1/items/:itemId"),
    ]
    assert scan.routes[0].param_mapping == (("itemId", ":itemId"),)
    assert scan.unsupported_patterns == []


def test_ambiguous_or_dynamic_prefixes_block_routes(tmp_path: Path) -> None:
    source = write(
        tmp_path,
        "server.js",
        """
        const express = require('express');
        const { Router } = require('express');
        const cors = require('cors');
        const config = require('./config');

        const app = express();
        const PREFIX = '/api';
        const router = Router();

        router.get('/orders', listOrders);
        app.use(PREFIX, router);
        app.use(`/v${config.version}`, router);
        app.use(['/a', '/b'], router);
        app.use(cors(), router);
        app.use('/inline', require('./inline'));

        function listOrders(req, res) { res.json([]); }
        """,
    )
    scan = scan_express_routes(tmp_path)

    assert [(route.method, route.path) for route in scan.routes] == [("GET", "/orders")]  # via cors()
    inline_line = line_of(source, "app.use('/inline'")
    assert scan.unsupported_patterns == [
        f"express:dynamic-prefix server.js:{line_of(source, 'app.use(PREFIX, router)')}",
        f"express:dynamic-prefix server.js:{line_of(source, 'app.use(`/v')}",
        f"express:unsupported-path-syntax server.js:{line_of(source, 'app.use([')}",
        f"express:cross-file-router server.js:{inline_line}",
    ]


def test_settings_reads_cycles_and_unmounted_routers(tmp_path: Path) -> None:
    source = write(
        tmp_path,
        "app.js",
        """
        const express = require('express');
        const app = express();
        const a = express.Router();
        const b = express.Router();
        const spare = express.Router();

        const env = app.get('env');
        app.set('title', 'payments');
        a.get('/a', handlerA);
        b.get('/b', handlerB);
        spare.get('/spare', handlerA);
        a.use('/b', b);
        b.use('/a', a);
        app.use('/a', a);
        """,
    )
    scan = scan_express_routes(tmp_path)

    assert [(route.path, route.handler) for route in scan.routes] == [
        ("/a/a", "handlerA"),
        ("/a/b/b", "handlerB"),
    ]
    cycle_line = line_of(source, "b.use('/a', a)")
    assert scan.unsupported_patterns == [
        f"express:router-not-mounted app.js:{line_of(source, 'const spare')}",
        f"express:mount-cycle app.js:{cycle_line}",
    ]


def test_chained_registrations_are_extracted(tmp_path: Path) -> None:
    source = write(
        tmp_path,
        "server.js",
        """
        const express = require('express');
        const cors = require('cors');
        const app = express();
        const router = express.Router();

        app.get('/v1/a', handlerA).post('/v1/b', handlerB);
        router
          .get('/c', handlerA)
          .put('/d', handlerB);
        app.use(cors()).use('/r', router).delete('/v1/e', handlerA);

        function handlerA(req, res) { res.json({}); }
        function handlerB(req, res) { res.json({}); }
        """,
    )
    scan = scan_express_routes(tmp_path)

    chain_line = source.read_text(encoding="utf-8").splitlines().index("router") + 1
    first, uses = line_of(source, "app.get('/v1/a'"), line_of(source, "app.use(cors())")
    assert [(route.method, route.path, route.line_start, route.excerpt) for route in scan.routes] == [
        ("GET", "/r/c", chain_line, "router .get('/c', handlerA)"),
        ("PUT", "/r/d", line_of(source, ".put('/d'"), ".put('/d', handlerB)"),
        ("GET", "/v1/a", first, "app.get('/v1/a', handlerA)"),
        ("POST", "/v1/b", first, ".post('/v1/b', handlerB)"),  # Express returns the receiver
        ("DELETE", "/v1/e", uses, ".delete('/v1/e', handlerA)"),
    ]
    assert scan.unsupported_patterns == []


def test_routes_on_receivers_that_are_not_apps_of_the_file_are_reported(tmp_path: Path) -> None:
    source = write(
        tmp_path,
        "app.ts",
        """
        import express, { Router } from 'express';
        const createRouter = require('./make-router');
        const v2 = require('./v2');

        const api = createRouter();
        api.get('/v1/payments/:id', getPayment);
        api.post('/v1/payments', (req, res) => res.json({}));
        v2.get('/v2/payments/:id', getPayment);

        class Server {
          public router = Router();
          constructor(private app = express()) {
            this.app.get('/v1/charges/:id', getPayment);
            this.router.post('/v1/charges', getPayment);
            this.app.route('/v1/refunds').get(getPayment).post(getPayment);
          }
        }

        export function register(app) {
          app.all('/v1/legacy', getPayment);
          app.get('/v1/a', getPayment).post('/v1/b', getPayment);
          app.use(express.json()).put('/v1/c', getPayment);
        }

        function getPayment(req, res) { res.json({}); }
        """,
    )
    scan = scan_express_routes(tmp_path)

    needles = [
        "api.get('/v1/payments/:id'",  # ":param" segment: route syntax
        "api.post('/v1/payments'",  # declared in the file and handed a function
        "v2.get(",
        "this.app.get(",  # member receivers named like a router
        "this.router.post(",
        "this.app.route(",  # one pattern, two registrations
        "app.all(",
        "app.get('/v1/a'",  # the chained .post counts too
        "app.use(express.json())",
    ]
    assert scan.routes == []
    assert scan.unsupported_patterns == [
        f"express:unresolved-router app.ts:{line_of(source, needle)}" for needle in needles
    ]
    assert "(unresolved router: 11)" in scan.warnings[0]


def test_http_client_requests_are_not_reported(tmp_path: Path) -> None:
    write(
        tmp_path,
        "client.js",
        """
        const axios = require('axios');
        const ledger = axios.create({ baseURL: 'http://ledger.internal' });

        async function sync(entry, payload) {
          await axios.get('/v1/entries', { params: { id: entry.id } });
          await axios.post('/v1/notify', payload);
          await ledger.post('/v1/entries', entry);
          $.get('/v1/status', function (data) { return data; });
          return ledger.get('/v1/entries/' + entry.id, { timeout: 5 });
        }
        """,
    )
    scan = scan_express_routes(tmp_path)

    assert (scan.routes, scan.unsupported_patterns, scan.warnings) == ([], [], [])


# Lexer robustness and bounds -----------------------------------------------------------


def test_lexer_handles_regexes_templates_comments_and_crlf(tmp_path: Path) -> None:
    source = textwrap.dedent(
        """
        const express = require('express');
        const app = express();
        const quote = /['"`]/g; // a regex literal with quote characters
        const slash = /\\/\\*not a comment*\\//;
        const ratio = 10 / 2 / 5;
        const nested = `outer ${`inner ${ratio} app.get('/nested-fake', x)`} done`;
        const quoted = `'${String(ratio).replace(/'/g, "")}' app.get('/quoted-fake', x)`;
        /* app.get('/block-fake', x);
           still a comment */
        app.get('/after', handler); // app.get('/line-fake', x)
        """
    )
    (tmp_path / "server.cjs").write_bytes(source.replace("\n", "\r\n").encode("utf-8"))
    scan = scan_express_routes(tmp_path)

    expected_line = source.splitlines().index("app.get('/after', handler); // app.get('/line-fake', x)") + 1
    assert [(route.path, route.line_start) for route in scan.routes] == [("/after", expected_line)]
    assert scan.unsupported_patterns == []


def test_untokenizable_files_are_skipped_with_a_reason(tmp_path: Path) -> None:
    write(tmp_path, "broken.js", "const s = 'unterminated;\napp.get('/x', h);\n")
    write(tmp_path, "types.d.ts", "declare const app: unknown;\n")
    write(tmp_path, "node_modules/express/index.js", "app.get('/vendored', h);\n")
    scan = scan_express_routes(tmp_path)

    assert scan.routes == []
    assert scan.files_scanned == 0
    assert scan.files_skipped == 1
    assert scan.warnings[0] == (
        "broken.js: skipped (could not be tokenized: unterminated string literal at line 1)"
    )


def test_symlinked_sources_outside_the_root_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside, "secret.js", "const app = require('express')();\napp.get('/secret', h);\n")
    root = tmp_path / "root"
    write(root, "server.js", "const app = require('express')();\napp.get('/inside', h);\n")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (root / "linked.js").symlink_to(outside / "secret.js")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported here")
    scan = scan_express_routes(root)

    assert route_keys(scan) == [("GET", "/inside")]
    assert (scan.files_scanned, scan.files_skipped) == (1, 1)
    assert "linked.js: skipped (symbolic link not followed)" in scan.warnings


def test_mount_graph_budget_stops_the_walk_and_reports_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = ["const app = require('express')();", "const express = require('express');"]
    lines += ["const router = express.Router();", "const other = express.Router();"]
    lines += ["router.get('/a', h);", "router.get('/b', h);", "other.get('/c', h);"]
    lines += [f"app.use('/p{index}', router);" for index in range(10)]
    lines += ["app.use('/z', other);", "function h(req, res) {}"]
    source = write(tmp_path, "app.js", "\n".join(lines) + "\n")
    # Entering the app costs 1 + 11 mounts; each entry into ``router`` 1 + 2 routes.
    monkeypatch.setattr(source_express, "_MAX_GRAPH_WORK", 18)
    scan = scan_express_routes(tmp_path)

    assert route_keys(scan) == [("GET", "/p0/a"), ("GET", "/p0/b"), ("GET", "/p1/a"), ("GET", "/p1/b")]
    stopped = line_of(source, "'/p2'")  # the mount the walk could no longer afford
    assert scan.unsupported_patterns == [f"express:mount-graph-limit app.js:{stopped}"]
    assert "(mount graph limit: 1)" in scan.warnings[0]  # /z/c: never reached, not "unmounted"
    assert scan.warnings[1] == "express: mount graph too large; scan truncated during route resolution"


def test_excerpts_read_a_bounded_window_of_long_registrations(tmp_path: Path) -> None:
    blank_lines = "\n" * (2 * source_files.EXCERPT_WINDOW)
    write(tmp_path, "app.js", f"const app = require('express')();\napp.get('/big',{blank_lines} h);\n")
    (route,) = scan_express_routes(tmp_path).routes

    assert route.excerpt == "app.get('/big', ..."  # cut at the window and marked, not silently
    assert route.line_end - route.line_start == 2 * source_files.EXCERPT_WINDOW


PATHOLOGICAL_BODIES = {
    "nested_calls": "app.get('/a', " * 16000 + "h" + ")" * 16000 + ";",  # excerpts overlapped
    "nested_mounts": "app.use(" * 3000 + "h" + ", h)" * 3000 + ";",  # "=>" rescans
    "unclosed_regexes": "const x = " + "(/[" * 4000,  # every "/" rescanned the line
    "many_mounts": "".join(f"r.get('/a{i}', h);\n" for i in range(1200)) + "app.use('/m', r);\n" * 1200,
}


@pytest.mark.parametrize("name", sorted(PATHOLOGICAL_BODIES))
def test_pathological_files_scan_in_bounded_time(tmp_path: Path, name: str) -> None:
    body = PATHOLOGICAL_BODIES[name]
    header = "const express = require('express');\nconst app = express();\nconst r = express.Router();\n"
    write(tmp_path, f"{name}.js", header + body + "\n")
    started = time.perf_counter()
    scan = scan_express_routes(tmp_path)

    assert time.perf_counter() - started < 2.0  # each took 3 s to 36 s before the bounds
    if name == "unclosed_regexes":  # skipped and counted, never guessed
        assert scan.files_skipped == 1
        assert scan.warnings[0].startswith(f"{name}.js: skipped (could not be tokenized: regular expression")


def test_bounds_and_root_validation(tmp_path: Path) -> None:
    for name in ("a.js", "b.ts", "c.mts"):
        write(tmp_path, name, "const x = 1;\n")
    scan = scan_express_routes(tmp_path, max_files=1)

    assert (scan.files_scanned, scan.files_skipped) == (1, 2)
    assert scan.warnings[0] == "scan truncated: 2 file(s) beyond max_files=1 were not scanned"
    with pytest.raises(DiscoveryError):
        scan_express_routes(tmp_path / "a.js")
    with pytest.raises(DiscoveryError):
        scan_express_routes(tmp_path, max_file_bytes=0)
