"""Shockwave native scenario harness: Stripe SDK cases (Python consumers).

Staged by ``verification.scenarios.staging`` as
``.shockwave/scenarios/<scenario_id>/test_shockwave_stripe_cases.py`` next to
``binding.json`` inside the runner's disposable checkout, and executed natively with
``python -m pytest`` and the hermetic arguments ``staging.runner_argv_hint`` documents. It
runs in the consumer's environment, so it imports only the standard library at module level
and avoids syntax newer than Python 3.8.

Protocol (``binding.json``, ``http_injection: "stripe_sdk_env"``): the binding names the
consumer's real entry point and its cases (arguments, the field read from the result, the
invariant expected value, and the PaymentIntent ids the call must request). The runner
environment provides SHOCKWAVE_SCENARIO_ID, SHOCKWAVE_SCENARIO_PHASE, SHOCKWAVE_FIXTURE_ID,
SHOCKWAVE_FIXTURE_PATH (the fixture set whose ``responses`` are served),
SHOCKWAVE_REPORT_PATH and SHOCKWAVE_REPO_ROOT.

The harness starts an in-process loopback fixture server (a simulation of Stripe's
``GET /v1/payment_intents/{intent}`` responses; not Stripe, not another provider) on
127.0.0.1 and an ephemeral port. Known ids get the fixture body; unknown ids get a
Stripe-style 404 ``resource_missing`` error; other paths get 404 and other methods on the
operation path 405. Every request is recorded: method, path, the Stripe-Version header and
only the KIND of the Authorization header ("test_mode_bearer" for "Bearer sk_test_...",
"missing" or "other"); the key itself is never recorded. Before importing the consumer
module, the harness sets the binding's key variable (e.g. STRIPE_SECRET_KEY) to a dummy
test-mode key and its API base variable (e.g. STRIPE_API_BASE) to the server URL, so the
consumer's own code constructs the official Stripe SDK client against the fixture server.

Network guard: best-effort, in-process interception, NOT network isolation. After the
fixture server starts, and for the rest of the process, socket.socket's connect and
connect_ex and the socket module's create_connection and getaddrinfo refuse every target
except 127.0.0.1 on the fixture server's own port; sendto, sendmsg, gethostbyname,
gethostbyname_ex and gethostbyaddr always refuse (ConnectionRefusedError). Raw ``_socket``
sockets, references taken before the guard, native code and child processes are not
covered, so the runner must isolate the network for every profile; the guard only keeps the
consumer's ordinary HTTP calls on the fixture server.

Every case is evaluated (one failure does not skip the others). A case is "failed" only
when its comparison ran and the value differs from the expectation; an exception, a missing
result field or a non-JSON result is "error". The report is always written (try/finally),
and the test fails iff any case did not pass. Shockwave, not this harness, judges the
recorded requests: a case without a request, a request other than GET
/v1/payment_intents/<the case's ids>, a missing Stripe-Version or non-test-mode auth makes
the run inconclusive.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import socket
import socketserver
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote

SCHEMA_VERSION = "shockwave.v1"
REPORT_KIND = "scenario_report"
REPORT_FORMAT = "shockwave.cases.v1"
OFFLINE_TEST_KEY = "sk_test_shockwave_offline"  # dummy test-mode key; never a real credential
LOOPBACK_HOST = "127.0.0.1"
MAX_MESSAGE_CHARS = 500
MAX_PATH_CHARS = 200
MAX_HEADER_CHARS = 64
MAX_RECORDED_REQUESTS = 64
MAX_RESULT_CHARS = 4000
MAX_BODY_BYTES = 1024 * 1024
HARNESS_FILE = os.path.abspath(__file__)
BINDING_FILE = os.path.join(os.path.dirname(HARNESS_FILE), "binding.json")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _bounded(text, limit=MAX_MESSAGE_CHARS):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        raise KeyError(f"{name} is not set by the runner")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _authorization_kind(value):
    if not value:
        return "missing"
    scheme, _, credential = value.partition(" ")
    if scheme.lower() == "bearer" and credential.startswith("sk_test_"):
        return "test_mode_bearer"
    return "other"


def _operation_pattern(path_template):
    segments = [
        "([^/]+)" if segment.startswith("{") and segment.endswith("}") else re.escape(segment)
        for segment in path_template.strip("/").split("/")
    ]
    return re.compile("^/" + "/".join(segments) + "$")


class _RequestLog:
    """Requests the fixture server received, in order (bounded; thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []
        self.dropped = 0

    def record(self, entry):
        with self._lock:
            if len(self._entries) >= MAX_RECORDED_REQUESTS:
                self.dropped += 1
            else:
                self._entries.append(entry)

    def mark(self):
        with self._lock:
            return len(self._entries)

    def since(self, start, end=None):
        with self._lock:
            return [dict(entry) for entry in self._entries[start:end]]


class _FixtureServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True


def _start_fixture_server(responses, path_template, log):
    """Serve ``responses`` for ``GET <path_template>`` on 127.0.0.1 (ephemeral port)."""
    pattern = _operation_pattern(path_template)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ShockwaveStripeFixture/1"
        sys_version = ""

        def _serve(self):
            path = self.path.split("?", 1)[0]
            log.record(
                {
                    "method": _bounded(self.command, 16),
                    "path": _bounded(path, MAX_PATH_CHARS),
                    "stripe_version": None
                    if self.headers.get("Stripe-Version") is None
                    else _bounded(self.headers.get("Stripe-Version"), MAX_HEADER_CHARS),
                    "authorization": _authorization_kind(self.headers.get("Authorization")),
                }
            )
            self._drain_body()
            match = pattern.match(path)
            if match is None:
                status, body = 404, _error(f"Unrecognized request URL ({self.command}: {_bounded(path, 120)}).")
            elif self.command != "GET":
                status, body = 405, _error(f"The fixture server only serves GET {path_template}.")
            else:
                resource_id = unquote(match.group(1))
                if resource_id in responses:
                    status, body = 200, responses[resource_id]
                else:
                    status, body = 404, _error(
                        f"No such payment_intent: '{_bounded(resource_id, 80)}'", code="resource_missing", param="intent"
                    )
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Request-Id", "req_shockwave_fixture")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _drain_body(self):
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
            except ValueError:
                length = 0
            if length > 0:
                self.rfile.read(length)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

        def log_message(self, *args):  # BaseHTTPRequestHandler's logging hook; the server stays silent
            return None

    server = _FixtureServer((LOOPBACK_HOST, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="shockwave-stripe-fixture", daemon=True)
    thread.start()
    return server, thread


def _error(message, code=None, param=None):
    error = {"type": "invalid_request_error", "message": message}
    if code is not None:
        error["code"] = code
    if param is not None:
        error["param"] = param
    return {"error": error}


def _refuse_network(*args, **kwargs):
    raise ConnectionRefusedError("shockwave harness: real network access is disabled")


def _port_number(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _install_network_guard(port):
    """Refuse every network target except 127.0.0.1:<port> for the rest of the process (never restored)."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo

    def allowed(address):
        return (
            isinstance(address, tuple)
            and len(address) == 2
            and address[0] == LOOPBACK_HOST
            and _port_number(address[1]) == port
        )

    def connect(self, address):
        if self.family == socket.AF_INET and allowed(address):
            return original_connect(self, address)
        return _refuse_network()

    def connect_ex(self, address):
        if self.family == socket.AF_INET and allowed(address):
            return original_connect_ex(self, address)
        return _refuse_network()

    def create_connection(address, *args, **kwargs):
        if allowed(tuple(address) if isinstance(address, (tuple, list)) else address):
            return original_create_connection(address, *args, **kwargs)
        return _refuse_network()

    def getaddrinfo(host, port_value, *args, **kwargs):
        if host == LOOPBACK_HOST and _port_number(port_value) == port:
            return original_getaddrinfo(host, port_value, *args, **kwargs)
        return _refuse_network()

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo
    for name in ("sendto", "sendmsg"):
        if hasattr(socket.socket, name):  # sendmsg is missing on some platforms
            setattr(socket.socket, name, _refuse_network)
    for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        setattr(socket, name, _refuse_network)


def _read_result(value, result_path):
    for key in result_path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif not isinstance(value, dict) and hasattr(value, key):
            value = getattr(value, key)
        else:
            raise LookupError(f"the result has no field {key!r}")
    return value


def _json_value(value, depth=0):
    """``value`` as JSON data; TypeError for anything else (e.g. a Decimal or an object)."""
    if depth > 32:
        raise TypeError("the result is nested too deeply")
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("the result is not a finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item, depth + 1) for key, item in value.items()}
    raise TypeError(f"the result is not JSON data ({type(value).__name__})")


def _value_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "True" if value else "False"
    return _canonical(value)


def _new_report():
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "report_format": REPORT_FORMAT,
        "scenario_id": os.environ.get("SHOCKWAVE_SCENARIO_ID"),
        "scenario_kind": None,
        "phase": os.environ.get("SHOCKWAVE_SCENARIO_PHASE"),
        "fixture_id": os.environ.get("SHOCKWAVE_FIXTURE_ID"),
        "fixture_sha256": None,
        "status": "error",
        "error_type": None,
        "message": "the scenario did not complete",
        "cases": [],
        "unattributed_requests": [],
        "requests_dropped": 0,
        "harness_sha256": None,
        "binding_sha256": None,
    }
    try:
        with open(HARNESS_FILE, "rb") as handle:
            report["harness_sha256"] = _sha256(handle.read())
    except OSError:
        pass
    return report


def _write_report(report):
    path = os.environ.get("SHOCKWAVE_REPORT_PATH")
    if not path:
        sys.stderr.write("shockwave harness: no report path was provided; the report was not written\n")
        return
    try:
        text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:  # never expected: values are converted before they are stored
        report = dict(report, status="error", error_type=type(exc).__name__, cases=[], message=_bounded(exc))
        text = json.dumps(report, indent=2, sort_keys=True, default=str)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")
    os.replace(temporary, path)


class _CaseError(Exception):
    """An exception raised while evaluating one case (the consumer call, its result, or the value)."""


def _actual_value(function, case):
    try:
        actual = _json_value(_read_result(function(*case["args"]), case["result_path"]))
        if len(_canonical(actual)) > MAX_RESULT_CHARS:
            raise ValueError(f"the result is larger than {MAX_RESULT_CHARS} characters of JSON")
    except Exception as exc:  # any consumer exception; SystemExit/KeyboardInterrupt still end the run
        raise _CaseError(exc) from exc
    return actual


def _evaluate_case(function, case):
    entry = {
        "case_id": case["case_id"],
        "relevant": case["relevant"],
        "expected": case["expected"],
        "actual": None,
        "status": "error",
        "error_type": None,
        "message": "the case did not complete",
        "requests": [],  # filled from the request log once the server has stopped
    }
    try:
        actual = _actual_value(function, case)
    except _CaseError as error:  # recorded per case; the remaining cases still run
        cause = error.__cause__
        entry["error_type"] = type(cause).__name__
        entry["message"] = _bounded(f"{type(cause).__name__}: {cause}")
        return entry
    entry["actual"] = actual
    entry["status"] = "passed" if _canonical(actual) == _canonical(case["expected"]) else "failed"
    field = ".".join(case["result_path"])
    label = f"{field}=" if field and not isinstance(case["expected"], str) else ""
    entry["message"] = _bounded(f"expected {label}{_value_text(case['expected'])}, actual {_value_text(actual)}")
    return entry


def _run_cases(report):
    """Serve the fixture set, call the consumer per case and fill ``report``. Exceptions mean "error"."""
    with open(BINDING_FILE, "rb") as handle:
        binding_raw = handle.read()
    report["binding_sha256"] = _sha256(binding_raw)
    binding = json.loads(binding_raw.decode("utf-8"))
    report["scenario_id"] = binding["scenario_id"]
    report["scenario_kind"] = binding["scenario_kind"]
    if os.environ.get("SHOCKWAVE_SCENARIO_ID") != binding["scenario_id"]:
        raise ValueError("SHOCKWAVE_SCENARIO_ID does not match binding.json")
    target = binding["binding"]
    if target["http_injection"] != "stripe_sdk_env":
        raise ValueError(f"unsupported http_injection {target['http_injection']!r}")

    with open(_required_env("SHOCKWAVE_FIXTURE_PATH"), "rb") as handle:
        fixture_raw = handle.read()
    report["fixture_sha256"] = _sha256(fixture_raw)
    fixture = json.loads(fixture_raw.decode("utf-8"))
    if fixture.get("fixture_set_id") != os.environ.get("SHOCKWAVE_FIXTURE_ID"):
        raise ValueError("the fixture set does not match SHOCKWAVE_FIXTURE_ID")
    responses = fixture["responses"]
    operation = binding["operation"]
    if operation["method"] != "GET":
        raise ValueError("the fixture server only simulates GET operations")

    log = _RequestLog()
    server, thread = _start_fixture_server(responses, operation["path"], log)  # before the guard
    windows = []  # (case entry, first request index, end index): requests received during its call
    try:
        host, port = server.server_address[:2]
        os.environ[target["api_key_env"]] = OFFLINE_TEST_KEY
        os.environ[target["api_base_env"]] = f"http://{host}:{port}"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = LOOPBACK_HOST  # never route the loopback via a proxy
        _install_network_guard(port)  # never restored: a thread that outlives a call is refused too
        sys.path.insert(0, _required_env("SHOCKWAVE_REPO_ROOT"))
        module = importlib.import_module(target["module"])
        function = getattr(module, target["function"])
        if not callable(function):
            raise TypeError(f"{target['module']}.{target['function']} is not callable")
        for case in binding["cases"]:
            start = log.mark()
            entry = _evaluate_case(function, case)
            windows.append((entry, start, log.mark()))
            report["cases"].append(entry)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        received = log.since(0)
        covered = set()
        for entry, start, end in windows:
            entry["requests"] = received[start:end]
            covered.update(range(start, end))
        report["unattributed_requests"] = [item for index, item in enumerate(received) if index not in covered]
        report["requests_dropped"] = log.dropped


class ShockwaveStripeCasesTest(unittest.TestCase):
    def test_stripe_scenario_cases(self):
        report = _new_report()
        try:
            _run_cases(report)
            failing = [case["case_id"] for case in report["cases"] if case["status"] != "passed"]
            report["status"] = "failed" if failing or not report["cases"] else "passed"
            report["message"] = _bounded(
                f"{len(report['cases']) - len(failing)} of {len(report['cases'])} cases passed"
                + (f"; not passed: {', '.join(failing)}" if failing else "")
            )
        except BaseException as exc:  # recorded for the report, then re-raised for the native runner
            report["status"] = "error"
            report["error_type"] = type(exc).__name__
            report["message"] = _bounded(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _write_report(report)
        self.assertEqual(report["status"], "passed", report["message"])


if __name__ == "__main__":
    unittest.main()
