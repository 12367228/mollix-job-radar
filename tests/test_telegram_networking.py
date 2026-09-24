import json
import logging
from urllib.parse import quote

import httpx
import pytest

from jobradar.config import Settings
from jobradar.http import PublicHTTPClient
from jobradar.logging_utils import sanitized_network_message
from jobradar.telegram import TelegramClient, TelegramError, get_me, get_recent_chats


@pytest.fixture
def config():
    return Settings(telegram_token="0" * 8 + ":" + "x" * 35, telegram_chat_id="123456")


@pytest.mark.parametrize("operation", ["getMe", "getUpdates", "sendMessage", "public_http"])
def test_httpx_environment_and_tls_defaults_are_not_overridden(config, monkeypatch, operation):
    real_client = httpx.Client
    calls, constructed = [], []

    def handler(request):
        calls.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        result = {"id": 101, "is_bot": True} if method == "getMe" else [] if method == "getUpdates" else {"message_id": 102}
        return httpx.Response(200, json={"ok": True, "result": result})

    def factory(**kwargs):
        # Regression guard for the real root cause, including proxy/SSL/transport overrides.
        assert kwargs.get("trust_env", True) is True
        assert kwargs.get("verify", True) is True
        assert not {"transport", "proxy", "proxies", "base_url"} & kwargs.keys()
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        assert client.trust_env is True
        assert client.timeout.as_dict() == dict(connect=20, read=20, write=20, pool=20)
        constructed.append(client)
        return client

    monkeypatch.setattr("jobradar.http.httpx.Client", factory)
    if operation == "getMe":
        assert get_me(config)["is_bot"] is True
    elif operation == "getUpdates":
        assert get_recent_chats(config) == []
    elif operation == "sendMessage":
        telegram = TelegramClient(config)
        telegram.send_message("test")
        telegram.close()
    else:
        public = PublicHTTPClient()
        public.close()
    assert len(constructed) == 1 and constructed[0].is_closed
    if operation != "public_http":
        assert calls[0].url.host == "api.telegram.org"
        assert calls[0].url.path == f"/bot{config.telegram_token}/{operation}"


def test_get_me_success_with_token_only(config):
    def handler(request):
        assert request.method == "GET" and not request.url.query
        return httpx.Response(200, json={"ok": True, "result": {"id": 101, "is_bot": True, "username": "radar_example_bot"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = get_me(Settings(telegram_token=config.telegram_token), client=client)
    assert result == {"id": 101, "is_bot": True, "username": "radar_example_bot"}


@pytest.mark.parametrize("endpoint", ["getMe", "getUpdates", "sendMessage"])
@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_safe_endpoint_specific_connection_diagnostics(config, caplog, endpoint, exception_type):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception_type(f"Connection failed for {request.url}; token={config.telegram_token}; chat={config.telegram_chat_id}", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, caplog.at_level(logging.INFO, logger="jobradar"):
        with pytest.raises(TelegramError, match="telegram_connection_failed"):
            if endpoint == "getMe":
                get_me(config, client=client)
            elif endpoint == "getUpdates":
                get_recent_chats(config, client=client)
            else:
                TelegramClient(config, client=client, sleep=lambda _: None).send_message("test")
    assert len(calls) == (3 if endpoint == "sendMessage" else 1)
    diagnostics = [json.loads(record.message) for record in caplog.records if record.name == "jobradar"]
    assert diagnostics and diagnostics[0]["telegram_endpoint"] == endpoint
    assert diagnostics[0]["exception_type"] == exception_type.__name__
    assert "Connection failed" in diagnostics[0]["message"]
    assert config.telegram_token not in caplog.text
    assert config.telegram_chat_id not in caplog.text
    assert "api.telegram.org/bot" not in caplog.text


@pytest.mark.parametrize("endpoint", ["getMe", "getUpdates", "sendMessage"])
@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_api_failures_never_log_response_body(config, caplog, endpoint, status):
    def handler(request):
        return httpx.Response(status, json={"ok": False, "description": config.telegram_token + " PRIVATE BODY",
                                           "parameters": {"retry_after": 100}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, caplog.at_level(logging.INFO, logger="jobradar"):
        with pytest.raises(TelegramError):
            if endpoint == "getMe":
                get_me(config, client=client)
            elif endpoint == "getUpdates":
                get_recent_chats(config, client=client)
            else:
                TelegramClient(config, client=client, sleep=lambda _: None).send_message("test")
    assert config.telegram_token not in caplog.text and "PRIVATE BODY" not in caplog.text
    assert any(json.loads(record.message).get("http_status") == status for record in caplog.records if record.name == "jobradar")


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"ok":true,"result":null}', b'{"ok":true,"result":{"id":true,"is_bot":true}}', b'{"ok":false,"description":"PRIVATE"}'])
def test_get_me_malformed_or_api_rejected(config, body, caplog):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
        with pytest.raises(TelegramError):
            get_me(config, client=client)
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"ok":true}', b'{"ok":false,"description":"PRIVATE"}'])
def test_send_malformed_response_is_not_retried(config, body, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TelegramError):
            TelegramClient(config, client=client, sleep=lambda _: None).send_message("test")
    assert len(calls) == 1 and "PRIVATE" not in caplog.text


def test_encoded_tokens_and_urls_are_redacted_before_truncation(config):
    url = f"https://api.telegram.org/bot{config.telegram_token}/getMe"
    message = f"failed {url!r} {quote(config.telegram_token, safe='')} {quote(quote(url, safe=''), safe='')}\n" + "x" * 700
    safe = sanitized_network_message(message, secrets=(config.telegram_token,))
    assert config.telegram_token not in safe and quote(config.telegram_token, safe='') not in safe
    assert "api.telegram.org" not in safe and "\n" not in safe
    assert len(safe) <= 500
