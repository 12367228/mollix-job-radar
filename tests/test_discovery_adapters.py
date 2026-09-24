from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from jobradar.config import Settings
from jobradar.http import PublicHTTPClient, SourceError
from jobradar.scoring import assess_project
from jobradar.sources import configured_sources
from jobradar.sources.fl_ru import FlRuAdapter
from jobradar.sources.habr_freelance import HabrFreelanceAdapter
from jobradar.sources.telegram_public import TelegramPublicAdapter, post_skip_reason


def test_fl_parser_public_fields(fixture_dir, now):
    adapter = FlRuAdapter()
    projects = adapter.parse((fixture_dir / "fl_ru.html").read_text(encoding="utf-8"), now=now)
    assert len(projects) == 2 and adapter.parse_errors == 1 and adapter.filtered_count == 1
    p = projects[0]
    assert p.source_id == "970001" and p.currency == "RUB" and p.budget_max == 12000
    assert p.responses_count == 4 and p.published_at == now - timedelta(hours=2, minutes=15)
    assert p.category == "Программирование" and "webhook" in p.description
    assert not p.publication_confirmed and not assess_project(p, now=now).eligible
    assert projects[1].published_at is None and not assess_project(projects[1], now=now).eligible


def test_habr_parser_contract_not_live_layout(fixture_dir, now):
    adapter = HabrFreelanceAdapter()
    projects = adapter.parse((fixture_dir / "habr_freelance.html").read_text(encoding="utf-8"), now=now)
    assert len(projects) == 2 and adapter.parse_errors == 1
    p = projects[0]
    assert p.source_id == "980001" and p.budget_min == 15000 and p.currency == "RUB"
    assert p.responses_count == 3 and p.published_at == now - timedelta(hours=1, minutes=30)
    assert not p.publication_confirmed and not assess_project(p, now=now).eligible
    assert projects[1].published_at is None and not assess_project(projects[1], now=now).eligible


def test_fl_pinned_card_does_not_inherit_unrelated_page_category(now):
    html = '<h1>Удаленная работа для программистов</h1><div id="project-item123" class="topprjpay">' \
           '<h2><a href="/projects/123/example.html">Юридическая консультация</a></h2>' \
           '<div class="b-post__foot"><span class="b-post__bold">Заказ</span><span class="text-gray-opacity-4">1 час назад</span></div></div>'
    p = FlRuAdapter().parse(html, now=now)[0]
    assert p.category is None and not assess_project(p, now=now).relevance_gate


def test_telegram_preview_parser(fixture_dir, now):
    adapter = TelegramPublicAdapter(("freelansim_ru",))
    projects = adapter.parse((fixture_dir / "telegram_public.html").read_text(encoding="utf-8"), now=now)
    assert len(projects) == 7 and adapter.parse_errors == 1 and adapter.filtered_count == 1
    p = projects[0]
    assert p.source == "telegram_public" and p.channel == "freelansim_ru"
    assert p.source_id == "freelansim_ru_960001" and p.url == "https://t.me/freelansim_ru/960001"
    assert p.title == "Разработать Telegram-бота на Python aiogram"
    assert p.published_at == now - timedelta(hours=1) and p.budget_min == 15000
    assert p.origin_kind == "direct_post" and not assess_project(p, now=now).eligible
    assert [p.discovery_skip_reason for p in projects[1:4]] == ["advertisement", "education", "full_time_vacancy"]
    for p in projects[1:6]:
        assert not assess_project(p, now=now).eligible
    assert projects[-1].origin_kind == "repost" and "freelance.ru/task/view/900001" in projects[-1].external_url


@pytest.mark.parametrize("text,skip", [
    ("#реклама Python API erid:example", "advertisement"),
    ("Бесплатный вебинар Python для новичков", "education"),
    ("Python full-time, в штат, 5/2", "full_time_vacancy"),
    ("Проектная работа: Python, фиксированный бюджет. Full-time на 3 дня", None),
    ("Разработать Telegram-бота для регистрации участников курса", None),
    ("Доработать API генерации рекламных материалов", None),
])
def test_telegram_post_type_filters(text, skip):
    assert post_skip_reason(text) == skip


@pytest.mark.parametrize("timestamp", ["", "bad", "2026-09-21", "2026-09-21T10:00:00"])
def test_telegram_never_infers_timestamp_without_timezone(timestamp, now):
    html = '<div class="tgme_widget_message" data-post="test_channel/1"><div class="tgme_widget_message_text">Python API</div>' \
           f'<a class="tgme_widget_message_date"><time datetime="{timestamp}">10:00</time></a></div>'
    p = TelegramPublicAdapter(("test_channel",)).parse(html, now=now)[0]
    assert p.published_at is None and not assess_project(p, now=now).eligible


@pytest.mark.parametrize("adapter", [FlRuAdapter(), HabrFreelanceAdapter(), TelegramPublicAdapter(("freelansim_ru",))])
@pytest.mark.parametrize("fixture", ["malformed.html", "challenge.html"])
def test_new_adapters_fail_closed(adapter, fixture, fixture_dir, now):
    with pytest.raises(SourceError):
        adapter.parse((fixture_dir / fixture).read_text(encoding="utf-8"), now=now)


def test_habr_robots_block_stops_before_listing(now):
    urls = []
    def handler(request):
        urls.append(str(request.url))
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        public = PublicHTTPClient(client=client, sleep=lambda _: None)
        with pytest.raises(SourceError, match="robots_disallowed"):
            HabrFreelanceAdapter().fetch(public, now=now)
    assert urls == ["https://freelance.habr.com/robots.txt"]


def test_telegram_uses_only_configured_web_previews_and_no_credentials(fixture_dir, now):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        assert request.url.host == "t.me" and request.method == "GET"
        assert not request.headers.get("cookie") and not request.headers.get("authorization")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /s/\n", headers={"Set-Cookie": "tracking=example"})
        assert request.url.path == "/s/freelansim_ru"
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=(fixture_dir / "telegram_public.html").read_text(encoding="utf-8"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(TelegramPublicAdapter(("freelansim_ru",)).fetch(PublicHTTPClient(client=client, sleep=lambda _: None), now=now)) == 7
    assert paths == ["/robots.txt", "/s/freelansim_ru"]


def test_disabled_sources_never_fetch():
    settings = Settings(freelance_ru_enabled=False, fl_ru_enabled=False, habr_freelance_enabled=False,
                        telegram_public_enabled=False, weblancer_enabled=False)
    for source in configured_sources(settings):
        assert not source.enabled and source.fetch(None, now=None) == []
