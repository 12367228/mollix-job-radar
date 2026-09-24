from dataclasses import replace
import json
import logging

from bs4 import BeautifulSoup
import httpx
import pytest

from jobradar.config import Settings
from jobradar.scoring import score_project
from jobradar.telegram import TelegramClient, TelegramError, format_budget, format_message


def settings():
    # Obviously synthetic and assembled so repository secret scanners stay useful.
    return Settings(telegram_token="0" * 8 + ":" + "x" * 35, telegram_chat_id="123456")


def client_for(handler, sleep=lambda _: None):
    return TelegramClient(settings(), client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleep)


def test_telegram_formatter(project, now):
    message = format_message(project, score_project(project, now=now))
    assert "🔥 БРАТЬ" in message
    for label in ("Бюджет:", "Опубликовано:", "Источник:", "Откликов:", "Почему подходит:", "Сложность:", "Рекомендуемая цена:", "ГОТОВЫЙ ОТКЛИК:", "Открыть заказ"):
        assert label in message
    assert "10 000–20 000 RUB" in message
    assert "Freelance.ru" in message
    assert project.url in message


def test_html_escaping_and_500_character_description(project, now):
    p = replace(project, title='<b>Python & "API"</b>', description="<script>" + "д" * 1000)
    message = format_message(p, score_project(p, now=now))
    assert "&lt;b&gt;Python &amp;" in message
    assert "<script>" not in message
    assert "д" * 500 not in message
    visible = BeautifulSoup(message, "html.parser").get_text()
    assert len(visible.encode("utf-16-le")) // 2 <= 4096


def test_worst_case_emoji_and_markup_stays_within_limit(project, now):
    p = replace(project, title="Python " + "🔥" * 500, description="<>&🔥" * 5000)
    text = BeautifulSoup(format_message(p, score_project(p, now=now)), "html.parser").get_text()
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_good_and_unknown_fields(project, now):
    from datetime import timedelta
    p = replace(project, description=None, title="Python", published_at=now - timedelta(hours=20), budget_min=5000, budget_max=5000, responses_count=None)
    text = format_message(p, score_project(p, now=now))
    assert "🟡 ПОСМОТРЕТЬ — 7/10" in text
    assert "Откликов: неизвестно" in text
    p = replace(p, published_at=None, budget_min=None, budget_max=None)
    assert "Бюджет: не указан" in format_message(p, score_project(p, now=now))


def test_risk_flags_visible_in_message_without_sending(project, now):
    p = replace(project, description="Обязательные кейсы. Senior only. Срок: 30 дней.")
    a = score_project(p, now=now)
    message = format_message(p, a)
    assert "LARGE_PROJECT" in message and "EXPERIENCE_GATE" in message
    assert "🔥 БРАТЬ" not in message


def test_send_payload(project, now):
    payloads = []
    def handler(request):
        payloads.append(json.loads(request.content))
        assert request.url.host == "api.telegram.org"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    client_for(handler).send_message(format_message(project, score_project(project, now=now)))
    assert payloads[0]["chat_id"] == "123456"
    assert payloads[0]["parse_mode"] == "HTML"
    assert payloads[0]["link_preview_options"]["is_disabled"] is True


def test_retry_confirmed_429():
    calls, sleeps = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}}) if len(calls) == 1 else httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    client_for(handler, sleeps.append).send_message("test")
    assert len(calls) == 2 and sleeps == [2]


def test_ambiguous_timeout_never_retries_or_logs_token(caplog):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(str(request.url), request=request)
    with caplog.at_level(logging.DEBUG), pytest.raises(TelegramError) as error:
        client_for(handler).send_message("test")
    assert error.value.uncertain and len(calls) == 1
    assert settings().telegram_token not in caplog.text + str(error.value)


@pytest.mark.parametrize("status,body,uncertain", [
    (400, {"ok": False, "description": "private server details"}, False),
    (403, {"ok": False}, False), (500, {"ok": False}, True),
    (200, {"ok": True}, True), (429, {"ok": False, "parameters": {"retry_after": 100}}, False),
])
def test_error_response_classification(status, body, uncertain):
    with pytest.raises(TelegramError) as error:
        client_for(lambda _: httpx.Response(status, json=body)).send_message("test")
    assert error.value.uncertain == uncertain
    assert "private server details" not in str(error.value)


def test_oversized_response_is_ambiguous():
    with pytest.raises(TelegramError) as error:
        client_for(lambda _: httpx.Response(200, content=b"x" * 65537)).send_message("test")
    assert error.value.uncertain
