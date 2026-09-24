import httpx
import pytest

from jobradar.http import PublicHTTPClient, RobotsPolicy, SourceError, USER_AGENT

HOSTS = frozenset({"freelance.ru"})
URL = "https://freelance.ru/task"


def public_client(handler, *, max_bytes=2097152, sleeps=None):
    return PublicHTTPClient(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=(sleeps.append if sleeps is not None else lambda _: None), max_bytes=max_bytes)


def test_robots_wildcards_longest_rule_and_specific_useragent():
    rules = RobotsPolicy("User-agent: *\nDisallow: /\nAllow: /task\nDisallow: *?page=*\n")
    assert rules.allowed(URL)
    assert not rules.allowed(URL + "?page=2")
    assert not rules.allowed("https://freelance.ru/login/")
    rules = RobotsPolicy("User-agent: *\nDisallow: /\nUser-agent: MollixJobRadar\nAllow: /task$\nDisallow: /\nCrawl-delay: 5\n")
    assert rules.allowed(URL) and not rules.allowed(URL + "/view/1")
    assert rules.delay == 5


def test_robots_disallow_prevents_listing_request():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /task", headers={"Content-Type": "text/plain"})
    with pytest.raises(SourceError, match="robots_disallowed"):
        public_client(handler).get_listing(URL, HOSTS)
    assert calls == ["/robots.txt"]


def test_user_agent_no_cookies_and_spacing():
    calls, sleeps = [], []
    def handler(request):
        calls.append(request.url.path)
        assert request.headers["User-Agent"] == USER_AGENT
        assert "cookie" not in request.headers
        return httpx.Response(200, text="User-agent: *\nAllow: /" if request.url.path.endswith("robots.txt") else "<html/>", headers={"Content-Type": "text/plain" if len(calls) == 1 else "text/html", "Set-Cookie": "tracking=discard"})
    assert public_client(handler, sleeps=sleeps).get_listing(URL, HOSTS) == "<html/>"
    assert len(calls) == 2 and sleeps[0] > 0


@pytest.mark.parametrize("status", [401, 403, 302])
def test_no_access_bypass_or_redirect_following(status):
    calls = []
    def handler(request):
        calls.append(request.url)
        return httpx.Response(status, headers={"Location": "https://127.0.0.1/private"})
    with pytest.raises(SourceError):
        public_client(handler)._get(URL, HOSTS)
    assert len(calls) == 1


def test_retry_temporary_get_error_with_backoff():
    calls, sleeps = [], []
    def handler(request):
        calls.append(request.url)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, text="ok", headers={"Content-Type": "text/html"})
    assert public_client(handler, sleeps=sleeps)._get(URL, HOSTS) == "ok"
    assert len(calls) == 3 and 1 in sleeps and 2 in sleeps


def test_long_retry_after_skips_without_retry():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})
    with pytest.raises(SourceError):
        public_client(handler)._get(URL, HOSTS)
    assert len(calls) == 1


def test_content_and_stream_size_bounds():
    with pytest.raises(SourceError, match="response_too_large"):
        public_client(lambda _: httpx.Response(200, content=b"x" * 101, headers={"Content-Type": "text/html"}), max_bytes=100)._get(URL, HOSTS)
    with pytest.raises(SourceError, match="unexpected_content_type"):
        public_client(lambda _: httpx.Response(200, text="{}", headers={"Content-Type": "application/json"}))._get(URL, HOSTS)


def test_stream_without_content_length_is_still_bounded():
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"a" * 60
            yield b"b" * 60
    with pytest.raises(SourceError, match="response_too_large"):
        public_client(lambda _: httpx.Response(200, stream=Chunks(), headers={"Content-Type": "text/html"}), max_bytes=100)._get(URL, HOSTS)


def test_network_errors_are_redacted_and_retried():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("do not log remote secrets", request=request)
    with pytest.raises(SourceError) as error:
        public_client(handler)._get(URL, HOSTS)
    assert str(error.value) == "network_error" and len(calls) == 3
