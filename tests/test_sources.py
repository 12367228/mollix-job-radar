from decimal import Decimal

import pytest

from jobradar.http import SourceError
from jobradar.sources import enabled_sources
from jobradar.sources.fl_ru import FlRuAdapter
from jobradar.sources.freelance_ru import FreelanceRuAdapter
from jobradar.sources.weblancer import WeblancerAdapter


def test_freelance_fixture(fixture_dir, now):
    projects = FreelanceRuAdapter().parse((fixture_dir / "freelance_ru.html").read_text(encoding="utf-8"), now=now)
    assert [p.source_id for p in projects] == ["900001", "900002", "900003"]
    p = projects[0]
    assert p.budget_min == Decimal(10000) and p.budget_max == Decimal(20000)
    assert p.currency == "RUB" and p.responses_count == 2 and p.views_count == 1234
    assert p.category == "Веб-разработка и IT"
    assert p.published_at.isoformat() == "2026-09-21T10:00:00+00:00"
    assert "Срок: 3 дня" in p.description
    assert projects[2].budget_min is None and projects[2].published_at is None
    assert "#" not in projects[2].url


def test_weblancer_fixture(fixture_dir, now):
    projects = WeblancerAdapter().parse((fixture_dir / "weblancer.html").read_text(encoding="utf-8"), now=now)
    assert [p.source_id for p in projects] == ["990001", "990002", "990003"]
    p = projects[0]
    assert p.budget_min == p.budget_max == Decimal(200) and p.currency == "USD"
    assert p.responses_count == 0 and p.views_count == 1234
    assert "Python" in p.category
    assert p.published_at is None  # A calendar day is insufficient for strict freshness.
    assert projects[1].budget_min is None
    assert "Нужно собрать" in p.description


@pytest.mark.parametrize("adapter", [FreelanceRuAdapter(), WeblancerAdapter()])
@pytest.mark.parametrize("file", ["malformed.html", "challenge.html"])
def test_malformed_and_challenge_fail_closed(adapter, file, fixture_dir, now):
    with pytest.raises(SourceError):
        adapter.parse((fixture_dir / file).read_text(encoding="utf-8"), now=now)


def test_duplicate_cards_collapsed(fixture_dir, now):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((fixture_dir / "freelance_ru.html").read_text(encoding="utf-8"), "html.parser")
    card = str(soup.select_one("article.task-card"))
    assert len(FreelanceRuAdapter().parse(card + card, now=now)) == 1


def test_disabled_fl_never_fetches():
    adapter = FlRuAdapter()
    adapter.enabled = False
    assert adapter.fetch(None, now=None) == []
    assert any(a.name == "fl_ru" for a in enabled_sources())


def test_card_missing_optionals_is_valid(now):
    text = '<article><h2><a href="/freelance/veb-programmirovanie-31/python-1/">Python</a></h2></article>'
    p = WeblancerAdapter().parse(text, now=now)[0]
    assert p.source_id == "1"
    assert p.published_at is None and p.responses_count is None and p.budget_min is None


def test_known_empty_freelance_listing(now):
    assert FreelanceRuAdapter().parse('<div class="task-feed-list"><div class="empty">Нет заказов</div></div>', now=now) == []


def test_weblancer_disabled_by_default_but_can_be_enabled():
    assert "weblancer" not in [s.name for s in enabled_sources()]
    assert "weblancer" in [s.name for s in enabled_sources(weblancer_enabled=True)]


def test_freelance_fetch_uses_public_it_filter_and_robots(fixture_dir, now):
    import httpx
    from jobradar.http import PublicHTTPClient
    paths = []
    def handler(request):
        paths.append(request.url.path)
        assert not request.headers.get("cookie") and not request.headers.get("authorization")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /login/\n")
        assert request.url.path == "/task"
        assert request.url.params.get_list("c[]") == ["4"] or request.url.params.get("q") in FreelanceRuAdapter.search_terms
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=(fixture_dir / "freelance_ru.html").read_text(encoding="utf-8"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        http = PublicHTTPClient(client=transport, sleep=lambda _: None)
        assert len(FreelanceRuAdapter().fetch(http, now=now)) == 3
    assert paths == ["/robots.txt"] + ["/task"] * 11


def test_freelance_keeps_subcategories(now):
    html = '<article class="task-card"><a class="task-card__title-link" href="/task/view/1">Доработка</a>' \
           '<div class="task-card__chips"><span class="task-chip task-chip--cat">Веб-разработка и IT</span>' \
           '<span class="task-chip">Python</span></div></article>'
    assert FreelanceRuAdapter().parse(html, now=now)[0].category == "Веб-разработка и IT / Python"
