import io
import pytest
from integrations.http import GitHubHTTPTransport
from integrations.github import GitHubError


class HTTPResponse(io.BytesIO):
    def __init__(self, status, data=b'{}', headers=None):
        super().__init__(data)
        self.status, self.headers = status, headers or {}


class Opener:
    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        return next(self.responses)


def test_archive_redirect_strips_authorization():
    client = GitHubHTTPTransport()
    client.opener = Opener([HTTPResponse(302, headers={'Location': 'https://codeload.github.com/team/demo/legacy.tar.gz/sha'}),
                            HTTPResponse(200, b'archive')])
    assert client.request('GET', '/repos/team/demo/tarball/sha', token='secret').data == b'archive'
    assert client.opener.requests[0].get_header('Authorization') == 'Bearer secret'
    assert client.opener.requests[1].get_header('Authorization') is None


@pytest.mark.parametrize('location', ['https://evil.example/archive', 'http://codeload.github.com/archive',
    'https://user:secret@codeload.github.com/archive', 'https://codeload.github.com:444/archive'])
def test_redirect_rejected(location):
    client = GitHubHTTPTransport()
    client.opener = Opener([HTTPResponse(302, headers={'Location': location})])
    with pytest.raises(GitHubError): client.request('GET', '/repos/team/demo/tarball/sha', token='secret')
    assert len(client.opener.requests) == 1


def test_bounded_response():
    client = GitHubHTTPTransport(max_bytes=2)
    client.opener = Opener([HTTPResponse(200, b'oversize')])
    with pytest.raises(GitHubError, match='too_large'):
        client.request('GET', '/repos/team/demo/tarball/sha', token='secret')
