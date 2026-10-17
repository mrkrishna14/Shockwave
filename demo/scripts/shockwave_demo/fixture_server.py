"""``fixture-server``: a standalone loopback server that SIMULATES Stripe responses.

It serves ``GET /v1/payment_intents/<id>`` from demo/stripe-fixtures/<variant>/payment_intents.json
on 127.0.0.1 only: 200 with the fixture body for a known id, a Stripe-style 404
``resource_missing`` error for an unknown id, 405 for any other method on that path and 404 for
any other path. It is a simulation of Stripe responses for offline runs - not Stripe, not Stripe
production source and not another provider - and the ``proposed`` variant is the SIMULATED change.
Point a consumer at it with ``STRIPE_API_BASE=http://127.0.0.1:<port>`` and any test-mode-looking
dummy key (never a real key). The Authorization header is never logged or stored.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shockwave_demo.common import (
    FIXTURE_SERVER_LABEL,
    FIXTURE_SETS_DIR,
    SIMULATED_LABEL,
    DemoError,
    read_json,
)

VARIANTS = ("baseline", "proposed")
LOOPBACK = "127.0.0.1"
_RETRIEVE = re.compile(r"^/v1/payment_intents/([^/]+)$")


@dataclass
class RequestLog:
    entries: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, entry: dict) -> int:
        with self.lock:
            self.entries.append(entry)
            return len(self.entries)


def load_variant(variant: str) -> dict:
    if variant not in VARIANTS:
        raise DemoError(f"variant must be one of {', '.join(VARIANTS)}")
    document = read_json(FIXTURE_SETS_DIR / variant / "payment_intents.json")
    if document.get("variant") != variant or not isinstance(document.get("responses"), dict):
        raise DemoError(f"demo/stripe-fixtures/{variant}/payment_intents.json is not a {variant} fixture set")
    return document


def _authorization_kind(value: str | None) -> str:
    if value is None:
        return "missing"
    return "test_mode_bearer" if value.startswith("Bearer sk_test_") else "other"


def make_server(variant: str, port: int = 0, *, log: RequestLog | None = None, echo=None) -> ThreadingHTTPServer:
    """A bound (not yet serving) loopback server for ``variant``; ``port=0`` picks a free port."""
    document = load_variant(variant)
    responses = document["responses"]
    requests = log if log is not None else RequestLog()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ShockwaveStripeFixture/1"
        sys_version = ""

        def _handle(self) -> None:
            path = self.path.split("?", 1)[0]
            match = _RETRIEVE.match(path)
            if match and self.command == "GET" and match.group(1) in responses:
                status, payload = 200, responses[match.group(1)]
            elif match and self.command == "GET":
                status, payload = 404, _error(f"No such payment_intent: '{match.group(1)}'", "resource_missing", "intent")
            elif match:
                status, payload = 405, _error(f"{self.command} is not supported by this fixture server", None, None)
            else:
                status, payload = 404, _error(f"Unrecognized request URL ({self.command}: {path})", None, None)
            number = requests.add(
                {
                    "method": self.command,
                    "path": path,
                    "stripe_version": self.headers.get("Stripe-Version"),
                    "authorization": _authorization_kind(self.headers.get("Authorization")),
                    "status": status,
                }
            )
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Request-Id", f"req_fixture_{number:06d}")
            self.send_header("Shockwave-Fixture", f"simulation; variant={variant}")
            self.end_headers()
            self.wfile.write(body)
            if echo is not None:
                echo(f"{self.command} {path} -> {status} (Stripe-Version {self.headers.get('Stripe-Version')})")

        do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _handle

        def log_message(self, *args: object) -> None:  # silence the default stderr access log
            return

    if not 0 <= port <= 65535:
        raise DemoError("port must be between 0 and 65535")
    return ThreadingHTTPServer((LOOPBACK, port), Handler)


def _error(message: str, code: str | None, param: str | None) -> dict:
    error = {"type": "invalid_request_error", "message": message}
    if code:
        error["code"] = code
    if param:
        error["param"] = param
    return {"error": error}


def serve(variant: str, port: int, *, max_requests: int | None = None, out=print) -> int:
    """Serve until interrupted (or until ``max_requests`` requests were answered)."""
    document = load_variant(variant)
    log = RequestLog()
    server = make_server(variant, port, log=log, echo=lambda line: out(f"  {line}"))
    host, bound = server.server_address[:2]
    out(f"Stripe fixture server - {FIXTURE_SERVER_LABEL}")
    if variant == "proposed":
        out(f"  {SIMULATED_LABEL}")
    out(f"  variant    {variant}: {document['fixture_set_id']} (API {document.get('api_version')})")
    out(f"  serving    GET /v1/payment_intents/<id> for {', '.join(sorted(document['responses']))}")
    out(f"  listening  http://{host}:{bound}")
    out(f"  use        STRIPE_API_BASE=http://{host}:{bound} STRIPE_SECRET_KEY=sk_test_<any dummy value>")
    try:
        if max_requests is None:
            server.serve_forever()
        else:
            while len(log.entries) < max_requests:
                server.handle_request()
    except KeyboardInterrupt:
        out("  stopped")
    finally:
        server.server_close()
    return 0
