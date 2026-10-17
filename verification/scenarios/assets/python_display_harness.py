"""Shockwave native regression scenario: the displayed-price invariant (Python consumers).

Staged by ``verification.scenarios.staging`` as
``.shockwave/scenarios/<scenario_id>/test_shockwave_display_invariant.py`` next to
``binding.json`` inside the runner's disposable checkout, and executed natively with
``python -m pytest`` and the hermetic arguments ``staging.runner_argv_hint`` documents
(``python -m unittest discover -s <that directory>`` works too). It runs in the
consumer's environment, so it imports only the standard library at module level and
avoids syntax newer than Python 3.8.

Protocol: ``binding.json`` names the consumer entry point and the invariant expectation
(the displayed price is $10.00 for equivalent current/proposed payment responses). The
runner environment provides SHOCKWAVE_SCENARIO_ID, SHOCKWAVE_SCENARIO_PHASE,
SHOCKWAVE_FIXTURE_ID, SHOCKWAVE_FIXTURE_PATH (provider response fixture whose
``response.body`` is served), SHOCKWAVE_REPORT_PATH and SHOCKWAVE_REPO_ROOT. The bound
function runs with every ``requests`` entry point replaced by a fake that serves the
fixture body and records the URL.

Network guard: best-effort, in-process interception, NOT network isolation. Before the
bound function runs, and for the rest of the process, socket.socket's connect,
connect_ex, sendto and sendmsg and the socket module's create_connection, getaddrinfo,
gethostbyname, gethostbyname_ex and gethostbyaddr raise ConnectionRefusedError. Raw
``_socket`` sockets, references taken before the guard, native code and child processes
are not covered, so the runner must isolate the network for every profile; the guard
only keeps the consumer's ordinary HTTP calls on the fake.

The scenario report is always written (try/finally). Its status is "failed" only when
the relevant comparison ran and the displayed value differs from the expectation.
Anything that prevents the comparison (missing dependency, syntax error, missing
function, any exception) is "error", which Shockwave interprets as inconclusive.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import socket
import sys
import unittest
from contextlib import ExitStack
from unittest import mock

SCHEMA_VERSION = "shockwave.v1"
REPORT_KIND = "scenario_report"
MAX_MESSAGE_CHARS = 500
HARNESS_FILE = os.path.abspath(__file__)
BINDING_FILE = os.path.join(os.path.dirname(HARNESS_FILE), "binding.json")


def _sha256_of_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _bounded(text):
    text = str(text)
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 3] + "..."


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        raise KeyError(f"{name} is not set by the runner")
    return value


class _FakeResponse:
    """The subset of ``requests.Response`` a JSON-reading consumer touches."""

    status_code = 200
    ok = True
    reason = "OK"
    encoding = "utf-8"

    def __init__(self, url, body):
        self.url = url
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(body)
        self.content = self.text.encode("utf-8")
        self._body = body

    def json(self, **kwargs):
        return copy.deepcopy(self._body)

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _refuse_network(*args, **kwargs):
    raise ConnectionRefusedError("shockwave harness: real network access is disabled")


# Best-effort, in-process interception, not network isolation (see the module docstring).
_NETWORK_ENTRY_POINTS = (
    (socket.socket, ("connect", "connect_ex", "sendto", "sendmsg")),
    (socket, ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr")),
)


def _install_network_guard():
    """Refuse the network entry points for the rest of the process (never restored)."""
    for owner, names in _NETWORK_ENTRY_POINTS:
        for name in names:
            if hasattr(owner, name):  # sendmsg is missing on some platforms
                setattr(owner, name, _refuse_network)


def _install_requests_fake(stack, body, requested_urls):
    import requests  # third-party, so imported only when the binding asks for it

    def serve(url):
        requested_urls.append(str(url))
        return _FakeResponse(str(url), body)

    def fake_get(url=None, *args, **kwargs):
        return serve(url)

    def fake_request(method=None, url=None, *args, **kwargs):
        return serve(url)

    def fake_session_get(self, url=None, *args, **kwargs):
        return serve(url)

    def fake_session_request(self, method=None, url=None, *args, **kwargs):
        return serve(url)

    def fake_session_send(self, request=None, *args, **kwargs):
        return serve(getattr(request, "url", request))

    # Session.request/send catch every other requests entry point (post, api.request, ...).
    for owner, name, fake in (
        (requests, "get", fake_get),
        (requests, "request", fake_request),
        (requests.Session, "get", fake_session_get),
        (requests.Session, "request", fake_session_request),
        (requests.Session, "send", fake_session_send),
    ):
        stack.enter_context(mock.patch.object(owner, name, fake))


_HTTP_INJECTIONS = {"python_requests_get": _install_requests_fake}


def _new_report():
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "scenario_id": os.environ.get("SHOCKWAVE_SCENARIO_ID"),
        "phase": os.environ.get("SHOCKWAVE_SCENARIO_PHASE"),
        "fixture_id": os.environ.get("SHOCKWAVE_FIXTURE_ID"),
        "assertion_id": None,
        "status": "error",
        "expected": None,
        "actual": None,
        "error_type": None,
        "message": "the scenario did not complete",
        "requested_urls": [],
        "harness_sha256": None,
        "binding_sha256": None,
    }
    try:
        report["harness_sha256"] = _sha256_of_file(HARNESS_FILE)
    except OSError:
        pass
    return report


def _write_report(report):
    path = os.environ.get("SHOCKWAVE_REPORT_PATH")
    if not path:
        sys.stderr.write("shockwave harness: no report path was provided; the report was not written\n")
        return
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _run_scenario(report):
    """Call the bound consumer function; return (actual, expected). Exceptions mean "error"."""
    with open(BINDING_FILE, "rb") as handle:
        binding_raw = handle.read()
    report["binding_sha256"] = hashlib.sha256(binding_raw).hexdigest()
    binding = json.loads(binding_raw.decode("utf-8"))
    report["scenario_id"] = binding["scenario_id"]
    report["assertion_id"] = binding["assertion_id"]
    expected = binding["expected_display"]
    report["expected"] = expected
    if os.environ.get("SHOCKWAVE_SCENARIO_ID") != binding["scenario_id"]:
        raise ValueError("SHOCKWAVE_SCENARIO_ID does not match binding.json")

    with open(_required_env("SHOCKWAVE_FIXTURE_PATH"), "rb") as handle:
        fixture = json.loads(handle.read().decode("utf-8"))
    if fixture.get("fixture_id") != os.environ.get("SHOCKWAVE_FIXTURE_ID"):
        raise ValueError("the fixture document does not match SHOCKWAVE_FIXTURE_ID")
    body = fixture["response"]["body"]

    target = binding["binding"]
    install = _HTTP_INJECTIONS.get(target["http_injection"])
    if install is None:
        raise ValueError(f"unsupported http_injection {target['http_injection']!r}")
    sys.path.insert(0, _required_env("SHOCKWAVE_REPO_ROOT"))
    with ExitStack() as stack:
        install(stack, body, report["requested_urls"])
        _install_network_guard()  # never restored: a thread that outlives the call is refused too
        module = importlib.import_module(target["module"])
        render = getattr(module, target["function"])
        actual = render(*target["args"])
    if not isinstance(actual, str):
        returned = type(actual).__name__
        raise TypeError(f"{target['module']}.{target['function']} returned {returned}, not a display string")
    return actual, expected


class ShockwaveDisplayInvariantTest(unittest.TestCase):
    def test_displayed_price_invariant(self):
        report = _new_report()
        try:
            actual, expected = _run_scenario(report)
            report["actual"] = actual
            report["status"] = "passed" if actual == expected else "failed"
            report["message"] = _bounded(f"expected {expected}, actual {actual}")
        except BaseException as exc:  # recorded for the report, then re-raised for the native runner
            report["status"] = "error"
            report["error_type"] = type(exc).__name__
            report["message"] = _bounded(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _write_report(report)
        self.assertEqual(actual, expected, report["message"])


if __name__ == "__main__":
    unittest.main()
