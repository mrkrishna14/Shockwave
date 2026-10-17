"""Local stand-in for Stripe's GET /v1/payment_intents/{intent}, used by the tests.

It serves canned PaymentIntent responses from a fixture file; it is a simulation of
Stripe responses for offline tests, not Stripe and not another payment provider.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_RETRIEVE_PATH = re.compile(r"^/v1/payment_intents/([A-Za-z0-9_]+)$")


@dataclass
class RecordedRequest:
    method: str
    path: str
    stripe_version: str | None
    authorization: str | None


@dataclass
class FixtureServer:
    base_url: str
    requests: list[RecordedRequest] = field(default_factory=list)


def serve_fixtures(fixture_path: Path):
    """Context manager yielding a running FixtureServer for ``fixture_path``."""
    responses = json.loads(Path(fixture_path).read_text(encoding="utf-8"))["responses"]
    recorded: list[RecordedRequest] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            path = self.path.split("?", 1)[0]
            recorded.append(
                RecordedRequest("GET", path, self.headers.get("Stripe-Version"), self.headers.get("Authorization"))
            )
            match = _RETRIEVE_PATH.match(path)
            body = responses.get(match.group(1)) if match else None
            if body is None:
                self._send(404, {"error": {"type": "invalid_request_error", "code": "resource_missing",
                                           "message": f"No such fixture: {path}"}})
            else:
                self._send(200, body)

        def _send(self, status: int, payload: object) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Request-Id", "req_fixture")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    class _Running:
        def __enter__(self) -> FixtureServer:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            host, port = self._server.server_address[:2]
            return FixtureServer(f"http://{host}:{port}", recorded)

        def __exit__(self, *exc: object) -> None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)

    return _Running()
