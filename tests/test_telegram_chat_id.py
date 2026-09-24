import json
import logging

import httpx
import pytest

from jobradar.__main__ import main
from jobradar.config import Settings
from jobradar.telegram import NO_CHAT_UPDATES, TelegramError, get_recent_chats


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Track deletion even when the process originally had no such environment key.
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "temporary-test-value")
        monkeypatch.delenv(name)
    token = "0" * 8 + ":" + "x" * 35
    path = tmp_path / ".env"
    path.write_text(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")
    return token, path


def mock_cli(monkeypatch, handler):
    # Exercise the entire CLI/config/HTTP/parser path with a local transport.
    real_client = httpx.Client
    clients = []

    def factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("jobradar.telegram.httpx.Client", factory)
    return clients


def test_cli_loads_dotenv_without_chat_id_and_prints_only_metadata(setup_env, monkeypatch, capsys, caplog):
    token, path = setup_env
    before = path.read_bytes()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.telegram.org"
        assert request.url.path == f"/bot{token}/getUpdates"
        assert dict(request.url.params) == {"limit": "100", "timeout": "0"}
        assert "cookie" not in request.headers
        return httpx.Response(200, json={"ok": True, "result": [{
            "update_id": 4,
            "message": {"date": 100, "text": token, "from": {"first_name": "DO NOT PRINT"},
                        "chat": {"id": 123456, "type": "private", "username": "owner_example", "first_name": "PRIVATE"}},
        }]})

    clients = mock_cli(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        assert main(["telegram-chat-id"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {"chat_id": 123456, "type": "private", "username": "owner_example"}
    assert token not in output.out + output.err + caplog.text
    assert "DO NOT PRINT" not in output.out
    assert len(calls) == 1 and clients[0].is_closed
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [".env"]


def test_latest_per_chat_first_and_optional_username(setup_env, monkeypatch, capsys):
    token, _ = setup_env
    updates = [
        {"update_id": 1, "message": {"date": 10, "chat": {"id": 42, "type": "private", "username": "old_name"}}},
        {"update_id": 3, "message": {"date": 30, "chat": {"id": -101, "type": "supergroup", "title": "DO NOT PRINT"}}},
        {"update_id": 2, "message": {"date": 20, "chat": {"id": 42, "type": "private", "username": "new_name"}}},
        {"update_id": 4, "channel_post": {"date": 40, "chat": {"id": -102, "type": "channel", "username": "channel_example"}}},
    ]
    mock_cli(monkeypatch, lambda _: httpx.Response(200, json={"ok": True, "result": updates}))
    assert main(["telegram-chat-id"]) == 0
    chats = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert chats == [
        {"chat_id": -102, "type": "channel", "username": "channel_example"},
        {"chat_id": -101, "type": "supergroup"},
        {"chat_id": 42, "type": "private", "username": "new_name"},
    ]


@pytest.mark.parametrize("updates", [[], [None, {}, {"message": None}, {"message": {"chat": []}}, {"poll": {"id": 1}}]])
def test_empty_or_no_message_updates_show_start_instruction(setup_env, monkeypatch, capsys, updates):
    mock_cli(monkeypatch, lambda _: httpx.Response(200, json={"ok": True, "result": updates}))
    assert main(["telegram-chat-id"]) == 0
    output = capsys.readouterr()
    assert output.out == NO_CHAT_UPDATES + "\n"
    assert output.err == ""


@pytest.mark.parametrize("status", [400, 401, 404, 409, 429, 500, 302])
def test_http_errors_hide_token_body_and_redirect(setup_env, monkeypatch, capsys, caplog, status):
    token, _ = setup_env
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.invalid/" + token},
                              json={"ok": False, "description": token + " SERVER BODY"})

    clients = mock_cli(monkeypatch, handler)
    assert main(["telegram-chat-id"]) == 2
    output = capsys.readouterr()
    combined = output.out + output.err + caplog.text
    assert token not in combined and "SERVER BODY" not in combined
    assert len(calls) == 1 and clients[0].is_closed
    if status == 409:
        assert "webhook" in combined


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_failure_hides_secret_url(setup_env, monkeypatch, capsys, caplog, exception):
    token, _ = setup_env

    def handler(request):
        raise exception(str(request.url), request=request)

    clients = mock_cli(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        assert main(["telegram-chat-id"]) == 2
    output = capsys.readouterr()
    assert token not in output.out + output.err + caplog.text
    assert clients[0].is_closed


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b'{"ok":false,"description":"PRIVATE"}', b'{"ok":true,"result":{}}'])
def test_malformed_response_is_not_dumped(setup_env, monkeypatch, capsys, caplog, body):
    mock_cli(monkeypatch, lambda _: httpx.Response(200, content=body))
    assert main(["telegram-chat-id"]) == 2
    output = capsys.readouterr()
    assert body.decode() not in output.out + output.err + caplog.text


@pytest.mark.parametrize("token", ["", "bad-token-value"])
def test_missing_or_invalid_token_makes_no_request(setup_env, monkeypatch, capsys, caplog, token):
    _, path = setup_env
    path.write_text(f"TELEGRAM_BOT_TOKEN={token}\n", encoding="utf-8")
    calls = []
    mock_cli(monkeypatch, lambda request: calls.append(request))
    assert main(["telegram-chat-id"]) == 2
    assert calls == []
    output = capsys.readouterr()
    if token:
        assert token not in output.out + output.err + caplog.text


def test_utf8_bom_from_windows_editor_is_supported(setup_env, monkeypatch, capsys):
    token, path = setup_env
    path.write_text(f"TELEGRAM_BOT_TOKEN={token}\n", encoding="utf-8-sig")
    mock_cli(monkeypatch, lambda _: httpx.Response(200, json={"ok": True, "result": []}))
    assert main(["telegram-chat-id"]) == 0
    assert capsys.readouterr().out == NO_CHAT_UPDATES + "\n"


def test_untrusted_metadata_cannot_echo_a_token_or_terminal_controls(setup_env, monkeypatch, capsys):
    token, _ = setup_env
    updates = [{"message": {"chat": chat}} for chat in [
        {"id": 1, "type": "private", "username": token},
        {"id": 2, "type": "group", "username": "bad\nusername"},
        {"id": True, "type": "private"},
        {"id": 0, "type": "private"},
        {"id": 3, "type": token},
    ]]
    mock_cli(monkeypatch, lambda _: httpx.Response(200, json={"ok": True, "result": updates}))
    assert main(["telegram-chat-id"]) == 0
    output = capsys.readouterr().out
    assert token not in output and "username" not in output
    assert len(output.splitlines()) == 2


def test_stream_size_limit_without_content_length(setup_env):
    token, _ = setup_env

    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(129):
                yield b"x" * 8192

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=LargeStream()))) as client:
        with pytest.raises(TelegramError, match="лимит"):
            get_recent_chats(Settings(telegram_token=token), client=client)


def test_response_read_deadline(setup_env):
    token, _ = setup_env
    times = iter([0, 46])
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True, "result": []}))) as client:
        with pytest.raises(TelegramError, match="времени"):
            get_recent_chats(Settings(telegram_token=token), client=client, clock=lambda: next(times))


def test_token_only_config_validation_does_not_require_chat_id():
    Settings(telegram_token="0" * 8 + ":" + "x" * 35).require_telegram_token()
