"""Bounded GitHub HTTPS transport. No credential-bearing redirect following."""
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from .github import Response, GitHubError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubHTTPTransport:
    def __init__(self, *, timeout=20, max_bytes=32 * 1024 * 1024):
        self.timeout, self.max_bytes = timeout, max_bytes
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, *, token, body=None):
        if not path.startswith('/') or path.startswith('//') or '\\' in path:
            raise GitHubError('invalid_api_path')
        url = 'https://api.github.com' + path
        headers = {'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {token}',
                   'User-Agent': 'shockwave-adapter', 'Content-Type': 'application/json'}
        payload = json.dumps(body).encode() if body is not None else None
        deadline = time.monotonic() + self.timeout
        try:
            for attempt in range(2):
                request = urllib.request.Request(url, data=payload, headers=headers, method=method)
                try:
                    response = self.opener.open(request, timeout=self.timeout)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status = response.status
                    if status in (301, 302, 307, 308):
                        location = response.headers.get('Location', '')
                        parsed = urlsplit(location)
                        if (attempt or method != 'GET' or '/tarball/' not in path or
                            parsed.scheme != 'https' or parsed.hostname != 'codeload.github.com' or
                            parsed.port not in (None, 443) or parsed.username or parsed.password):
                            raise GitHubError('unsafe_redirect')
                        url, headers = location, {'User-Agent': 'shockwave-adapter'}
                        continue
                    chunks = bytearray()
                    while True:
                        if time.monotonic() > deadline:
                            raise GitHubError('response_deadline_exceeded')
                        chunk = response.read1(min(65536, self.max_bytes + 1 - len(chunks)))
                        if not chunk:
                            break
                        chunks.extend(chunk)
                        if len(chunks) > self.max_bytes:
                            raise GitHubError('response_too_large')
                    raw = bytes(chunks)
                    data = raw if '/tarball/' in path and status == 200 else json.loads(raw or b'{}')
                    return Response(status, data, {k.lower(): v for k, v in response.headers.items()})
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise GitHubError('transport_unavailable_or_invalid_response') from None
        raise GitHubError('unexpected_redirect')
