from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from jobradar.normalization import clean_text, html_text, parse_budget, parse_count, parse_date, safe_url


def test_text_and_html_normalization():
    assert clean_text("  Python\u00a0 &amp;\nAPI\u200b  ") == "Python & API"
    assert html_text('<p>Python<br>API</p><script>secret()</script>') == "Python API"


@pytest.mark.parametrize("text, expected", [
    ("10 000–30 000 ₽ / заказ", (10000, 30000, "RUB")),
    ("от 5 000 руб.", (5000, None, "RUB")), ("до 200 $", (None, 200, "USD")),
    ("150<!-- --> $", (150, 150, "USD")), ("10,50 EUR", (Decimal("10.50"), Decimal("10.50"), "EUR")),
    ("10.000 ₽", (10000, 10000, "RUB")), ("5–10 тыс руб", (5000, 10000, "RUB")),
    ("Обсуждается индивидуально", (None, None, None)), ("1000", (None, None, None)),
    ("1 000 ₽ / час", (None, None, None)), ("1000 USD per month", (None, None, None)),
])
def test_budget(text, expected):
    assert parse_budget(text) == expected


@pytest.mark.parametrize("text, expected", [("0 заявок", 0), ("1 234 просмотра", 1234), ("12", 12), ("нет данных", None), ("-1", None)])
def test_counts(text, expected):
    assert parse_count(text) == expected


def test_dates(now):
    assert parse_date("21.09.2026 15:00", now) == now
    assert parse_date("2026-09-21T12:00:00Z", now) == now
    assert parse_date("час назад", now) == now - timedelta(hours=1)
    assert parse_date("2 дня назад", now) == now - timedelta(days=2)
    assert parse_date("вчера в 15:00", now) == now - timedelta(days=1)
    assert parse_date("31.02.2026", now) is None
    assert parse_date("сегодня в 99:99", now) is None
    assert parse_date(None, now) is None


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "http://freelance.ru/task", "https://freelance.ru.evil.test/",
    "https://user:password@freelance.ru/task", "https://127.0.0.1/", "https://freelance.ru:8080/",
    "https://freelance.ru\\@evil.test/", "https://freelance.ru/%0afoo", "https://freelance.ru:bad/",
])
def test_reject_unsafe_urls(url):
    with pytest.raises(ValueError):
        safe_url(url, allowed_hosts={"freelance.ru"})


def test_safe_url():
    assert safe_url("/task/view/1#x", base="https://freelance.ru/task", allowed_hosts={"freelance.ru"}) == "https://freelance.ru/task/view/1"


def test_model_normalizes_and_serializes(project):
    p = replace(project, title="  Python\n API  ", currency="rub")
    assert p.title == "Python API"
    assert p.key == "freelance_ru:123"
    assert p.to_dict()["budget_min"] == "10000"
    assert p.to_dict()["published_at"].endswith("+00:00")


@pytest.mark.parametrize("changes", [{"title": " "}, {"source_id": "a:b"}, {"source": "../bad"}, {"responses_count": -1}, {"responses_count": True}, {"budget_min": -1}, {"budget_min": Decimal("NaN")}, {"budget_min": 30000}, {"published_at": datetime(2026, 1, 1)}])
def test_model_rejects_bad_fields(project, changes):
    with pytest.raises(ValueError):
        replace(project, **changes)
