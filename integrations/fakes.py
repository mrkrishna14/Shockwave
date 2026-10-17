"""Explicit local doubles; never report live delivery or a fabricated PR URL."""
from .github import Response


class FakeGitHubTransport:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, path, *, token, body=None):
        self.calls.append((method, path, body))  # credentials deliberately omitted
        value = self.routes.get((method, path), Response(404, {}, {}))
        return value() if callable(value) else value


class FakeEmailTransport:
    def __init__(self, *, fail=False):
        self.fail, self.calls = fail, 0

    def send(self, recipient, url, idempotency_key):
        self.calls += 1
        if self.fail:
            raise RuntimeError('sensitive provider response must not escape')
        return 'development_inbox', 'fake-local-receipt'


from types import SimpleNamespace


class ContractDouble(SimpleNamespace):
    """Test-only field container; shared.schemas remains canonical and required in production."""
    def model_copy(self, *, update):
        return type(self)(**(vars(self) | update))


def fake_models(name, **fields):
    return ContractDouble(**fields)
