from dataclasses import replace
from datetime import timedelta
import json

import httpx
import pytest

from jobradar.__main__ import check_candidates, main, print_candidate
from jobradar.config import Settings
from jobradar.http import PublicHTTPClient, SourceError
from jobradar.runner import RunResult, execute
from jobradar.scoring import assess_project
from jobradar.sources.fl_ru import FlRuAdapter
from jobradar.sources.freelance_ru import FreelanceRuAdapter
from jobradar.state import StateError, StateStore
from jobradar.telegram import format_message


@pytest.mark.parametrize("age,label", [(0, "FIRE"), (6, "FIRE"), (6.000001, "GOOD"), (24, "GOOD"), (24.000001, "SKIP")])
def test_hard_freshness_limits_cannot_be_disabled(project, now, age, label):
    p = replace(project, published_at=now - timedelta(hours=age))
    a = assess_project(p, now=now, max_project_age_hours=720, hot_project_age_hours=720)
    assert a.label == label
    if label == "GOOD":
        assert a.score <= 7 and "OLDER_THAN_HOT_WINDOW" in a.risk_flags


@pytest.mark.parametrize("changes", [
    {"published_at": None}, {"publication_confirmed": False}, {"publication_evidence": None},
    {"open_status": "closed"}, {"open_status": "awarded"}, {"open_status": "not_accepting"},
    {"url": "https://freelance.ru/task?c%5B%5D=4"}, {"url": "https://aggregator.invalid/task/view/123"},
    {"url": "https://freelance.ru/task/view/124"},
])
def test_safety_failures_override_high_raw_score(project, now, changes):
    a = assess_project(replace(project, **changes), now=now)
    assert a.raw_score >= 8 and a.score <= 5 and not a.eligible and a.label == "SKIP"


def test_unknown_open_status_is_reported_not_invented(project, now):
    a = assess_project(replace(project, open_status="unknown"), now=now)
    assert a.eligible and "OPEN_STATUS_UNKNOWN" in a.risk_flags


def test_notification_requires_original_card_and_strips_query(project, now):
    p = replace(project, url=project.url + "?utm_source=channel&ref=example")
    text = format_message(p, assess_project(p, now=now))
    assert f'href="{project.url}"' in text and "utm_source" not in text
    with pytest.raises(ValueError, match="missing_direct_project_url"):
        format_message(replace(project, url="https://freelance.ru/task"), assess_project(project, now=now))


def fl_project(fixture_dir, now):
    return FlRuAdapter().parse((fixture_dir / "fl_ru.html").read_text(encoding="utf-8"), now=now)[0]


def card_html(date="21.09.2026 в 14:00", status="", brief="Нужно закрыть проект в нашей CRM"):
    return f'<div id="projectp970001" class="fl-project-content__description-text">{brief}</div>' \
           f'<div class="text-10 opacity-50">Опубликован {date}</div>' \
           f'<div data-id="qa-order-status">{status}</div><a id="reply_offer">Откликнуться</a>'


def test_fl_requires_original_publication_not_raised_listing_age(fixture_dir, now):
    adapter = FlRuAdapter()
    p = fl_project(fixture_dir, now)
    assert not p.publication_confirmed and not assess_project(p, now=now).eligible
    verified = adapter.confirm_card(p, card_html(), now=now)
    assert verified.publication_confirmed and verified.published_at == now - timedelta(hours=1)
    assert verified.open_status == "open" and "Опубликован" in verified.publication_evidence
    assert assess_project(verified, now=now).label == "FIRE"
    old = adapter.confirm_card(p, card_html("20.09.2026 в 14:00"), now=now)
    assert assess_project(old, now=now).label == "SKIP"


@pytest.mark.parametrize("button", [
    '<a id="reply_offer" disabled>Откликнуться</a>',
    '<a id="reply_offer" aria-disabled="true">Откликнуться</a>',
    '<a id="reply_offer" hidden>Откликнуться</a>',
    '<div aria-hidden="true"><a id="reply_offer">Откликнуться</a></div>',
    '<div style="display: none"><a id="reply_offer">Откликнуться</a></div>',
    '<a id="reply_offer" class="disabled">Откликнуться</a>', '',
])
def test_unavailable_offer_control_does_not_confirm_open(fixture_dir, now, button):
    html = card_html().replace('<a id="reply_offer">Откликнуться</a>', button)
    p = FlRuAdapter().confirm_card(fl_project(fixture_dir, now), html, now=now)
    a = assess_project(p, now=now)
    assert p.open_status == "unknown" and a.score <= 7
    assert "OPEN_STATUS_UNKNOWN" in a.risk_flags


@pytest.mark.parametrize("date", ["", "вчера", "21.09.2026", "99.99.2026 в 14:00"])
def test_unreadable_original_date_cannot_reuse_listing_estimate(fixture_dir, now, date):
    p = FlRuAdapter().confirm_card(fl_project(fixture_dir, now), card_html(date), now=now)
    assert p.published_at is None and not p.publication_confirmed and not assess_project(p, now=now).eligible


def test_updated_date_and_ambiguous_publication_are_untrusted(fixture_dir, now):
    adapter, p = FlRuAdapter(), fl_project(fixture_dir, now)
    for html in [card_html().replace("Опубликован", "Обновлен"),
                 card_html() + '<div class="text-10 opacity-50">Опубликован 21.09.2026 в 13:00</div>']:
        checked = adapter.confirm_card(p, html, now=now)
        assert not checked.publication_confirmed and checked.published_at is None
    with pytest.raises(SourceError, match="unrecognized_project_card"):
        adapter.confirm_card(p, card_html().replace("projectp970001", "projectp970002"), now=now)


@pytest.mark.parametrize("published,label", [("21.09.2026 в 14:00", "FIRE"), ("20.09.2026 в 14:00", "SKIP")])
def test_publication_and_last_edit_in_same_field_use_original_date(fixture_dir, now, published, label):
    # Observed on live FL.ru cards: publication and last edit share one element.
    html = card_html(published + " Последнее изменение: 21.09.2026 в 14:59")
    p = FlRuAdapter().confirm_card(fl_project(fixture_dir, now), html, now=now)
    assert p.publication_confirmed and p.published_at.minute == 0
    assert assess_project(p, now=now).label == label


@pytest.mark.parametrize("label,status", [
    ("Исполнитель уже выбран", "awarded"), ("Исполнитель выбран", "awarded"),
    ("Проект закрыт", "closed"), ("Заказ закрыт", "closed"), ("Отклики больше не принимаются", "not_accepting"),
    ("Приём откликов завершён", "not_accepting"),
])
def test_closed_status_blocks_even_with_reply_button(fixture_dir, now, label, status):
    p = FlRuAdapter().confirm_card(fl_project(fixture_dir, now), card_html(status=label), now=now)
    assert p.open_status == status and not assess_project(p, now=now).eligible


def test_status_words_in_brief_are_not_a_status(fixture_dir, now):
    p = FlRuAdapter().confirm_card(fl_project(fixture_dir, now), card_html(brief="Проект закрыт"), now=now)
    assert p.open_status == "open" and assess_project(p, now=now).eligible


def test_freelance_clock_field_and_closed_badge(fixture_dir, now):
    html = (fixture_dir / "freelance_ru.html").read_text(encoding="utf-8")
    p = FreelanceRuAdapter().parse(html, now=now)[0]
    assert p.publication_confirmed and "clock title:" in p.publication_evidence
    missing_clock = FreelanceRuAdapter().parse(html.replace("fa-clock-o", "fa-map-marker"), now=now)[0]
    assert not missing_clock.publication_confirmed and missing_clock.published_at is None
    closed = FreelanceRuAdapter().parse(html.replace('class="task-card__desc">',
        'class="task-card__desc"><span class="task-card__status">Исполнитель выбран</span>', 1), now=now)[0]
    assert closed.open_status == "awarded" and not assess_project(closed, now=now).eligible


def test_persistent_url_identity_survives_pruning_restart_and_title_change(tmp_path, project, now):
    path = tmp_path / "state.json"
    state = StateStore(path, limit=1)
    state.reserve(project, assess_project(project, now=now), now=now)
    state.mark_sent(project, now=now)
    second = replace(project, source_id="456", url="https://freelance.ru/task/view/456")
    state.reserve(second, assess_project(second, now=now), now=now)
    state.mark_sent(second, now=now)
    assert project.key not in state.entries
    loaded = StateStore(path, limit=1)
    copy = replace(project, source="telegram_public", source_id="test_channel_10", origin_kind="repost",
                   title="Совсем другой заголовок", url="https://t.me/test_channel/10", external_url=project.url + "?utm_source=tg")
    assert loaded.contains(copy) and loaded.contains(project)
    before = path.read_bytes()
    assert not loaded.reserve(copy, assess_project(copy, now=now), now=now + timedelta(days=3))
    assert before == path.read_bytes()
    records = json.loads(before)["fingerprints"]
    assert len(records) == 2
    assert {"source", "source_id", "normalized_url", "first_seen_at", "sent_at"} <= set(next(iter(records.values())))


def test_first_seen_is_stable_after_definite_failure_and_mark_sent_is_idempotent(tmp_path, project, now):
    state = StateStore(tmp_path / "state.json")
    state.observe(project, now=now)
    state.reserve(project, assess_project(project, now=now), now=now + timedelta(minutes=1))
    state.release(project)
    state = StateStore(state.path)
    assert not state.contains(project)
    state.reserve(project, assess_project(project, now=now), now=now + timedelta(minutes=2))
    state.mark_sent(project, now=now + timedelta(minutes=3))
    before = state.path.read_bytes()
    state.mark_sent(project, now=now + timedelta(minutes=4))
    assert before == state.path.read_bytes()
    record = next(iter(state.fingerprints.values()))
    assert record["first_seen_at"] == now.isoformat() and record["sent_at"] == (now + timedelta(minutes=3)).isoformat()


def test_legacy_migration_is_read_only_and_preserves_delivery_date(tmp_path, project, now):
    path = tmp_path / "state.json"
    legacy = {"version": 1, "entries": {project.key: {"source": project.source, "source_id": project.source_id,
              "status": "sent", "reserved_at": now.isoformat(), "sent_at": now.isoformat()}}}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before = path.read_bytes()
    state = StateStore(path)
    assert state.contains(project) and not state.save() and path.read_bytes() == before
    second = replace(project, source_id="456", url="https://freelance.ru/task/view/456")
    state.reserve(second, assess_project(second, now=now), now=now)
    loaded = StateStore(path)
    assert loaded.contains(project) and loaded.entries[project.key]["sent_at"] == now.isoformat()
    assert json.loads(path.read_text())["version"] == 2


@pytest.mark.parametrize("field,value", [("normalized_url", "https://bad.invalid/?utm_x=1"),
    ("status", "forgotten"), ("first_seen_at", "2026-01-01"), ("sent_at", None)])
def test_corrupt_permanent_history_fails_closed(tmp_path, project, now, field, value):
    state = StateStore(tmp_path / "state.json")
    state.reserve(project, assess_project(project, now=now), now=now)
    state.mark_sent(project, now=now)
    data = json.loads(state.path.read_text())
    next(iter(data["fingerprints"].values()))[field] = value
    state.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError):
        StateStore(state.path)


def test_top_order_limit_unknown_responses_and_columns(project, now, capsys):
    candidates = []
    for i in range(15):
        p = replace(project, source_id=str(i), url=f"https://freelance.ru/task/view/{i}",
                    published_at=now - timedelta(hours=i // 5), responses_count=None if i == 1 else 20-i)
        a = replace(assess_project(p, now=now), score=10 if i < 10 else 7)
        candidates.append((p, a, False))
    candidates.append((project, replace(assess_project(project, now=now), eligible=False), False))
    result = RunResult(candidates=list(reversed(candidates)))
    top = list(check_candidates(result, top=True, verbose=True))
    assert [p.source_id for p, _, _ in top] == ["4", "3", "2", "0", "1", "9", "8", "7", "6", "5"]
    for p, a, duplicate in top:
        print_candidate(p, a, duplicate)
    output = capsys.readouterr().out
    assert output.count("DECISION:") == 10 and "DECISION: SKIP" not in output
    for field in ("SOURCE:", "SOURCE_ID:", "TITLE:", "DIRECT_URL:", "PUBLISHED_AT:", "AGE_HOURS:",
                  "BUDGET:", "RESPONSES_COUNT:", "SCORE:", "DECISION:", "REASONS:", "RISK FLAGS:"):
        assert output.count(field) == 10


def test_check_top_json_never_instantiates_sender_or_writes_legacy_state(tmp_path, fixture_dir, monkeypatch, capsys, project, now):
    path = tmp_path / "state.json"
    path.write_text('{"version":1,"entries":{}}', encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("jobradar.__main__.Settings.from_env", lambda: Settings(state_path=path))
    def forbidden(*a, **kw):
        raise AssertionError("No network, sender or state mutation in fixture check")
    monkeypatch.setattr("jobradar.__main__.TelegramClient", forbidden)
    monkeypatch.setattr("jobradar.__main__.PublicHTTPClient", forbidden)
    monkeypatch.setattr(StateStore, "save", forbidden)
    assert main(["check", "--top", "--json", "--fixtures", str(fixture_dir), "--now", now.isoformat()]) == 0
    data = json.loads(capsys.readouterr().out)
    assert 0 < len(data["projects"]) <= 10 and all(p["assessment"]["label"] in {"FIRE", "GOOD"} for p in data["projects"])
    assert all(p["project"]["direct_url"] and p["project"]["publication_confirmed"] for p in data["projects"])
    assert before == path.read_bytes()


def test_delivery_rechecks_clock_after_discovery_and_before_send(tmp_path, project, now):
    class Source:
        name = project.source
        def fetch(self, client, *, now):
            return [replace(project, published_at=now - timedelta(hours=23, minutes=59))]
    class Sender:
        def send_message(self, message):
            pytest.fail("24-hour boundary crossed: must not send")
    ticks = iter([now, now, now + timedelta(minutes=2)])
    result = execute(Settings(), [Source()], None, StateStore(tmp_path / "state.json"),
                     telegram=Sender(), clock=lambda: next(ticks), sleep=lambda _: None)
    assert result.sent == 0


def test_fl_pagination_observed_links_only_max_three(fixture_dir, now):
    adapter = FlRuAdapter()
    adapter.discovery_urls = [adapter.discovery_urls[0]]
    paths = []
    html = (fixture_dir / "fl_ru.html").read_text(encoding="utf-8")
    # No detail calls in this pagination-only test: all publication dates missing.
    html = html.replace("2 часа 15 минут назад", "неизвестно")
    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        assert not request.headers.get("cookie") and not request.headers.get("authorization")
        base = "/projects/category/programmirovanie/"
        links = ''.join(f'<a href="{base}page-{p}/">{p}</a>' for p in range(2, 8))
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=html + links)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        projects = adapter.fetch(PublicHTTPClient(client=client, sleep=lambda _: None), now=now)
    assert len(projects) == 2 and adapter.pages_fetched == 3
    assert paths == ["/robots.txt", "/projects/category/programmirovanie/", "/projects/category/programmirovanie/page-2/",
                     "/projects/category/programmirovanie/page-3/"]
    malicious = '<a href="https://bad.invalid/projects/category/programmirovanie/page-2/">2</a>'
    assert adapter.next_page(malicious, adapter.discovery_urls[0], 1) is None


def test_freelance_robots_forbids_pagination_before_listing_request(fixture_dir, now):
    adapter, requests = FreelanceRuAdapter(), []
    adapter.search_terms = ()
    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*?page=*\nDisallow: /*&page=*\n")
        assert "page=" not in str(request.url)
        html = (fixture_dir / "freelance_ru.html").read_text(encoding="utf-8")
        html += '<a data-page="1" href="/task?c%5B0%5D=4&amp;page=2">2</a>'
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=html)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(adapter.fetch(PublicHTTPClient(client=client, sleep=lambda _: None), now=now)) == 3
    assert len(requests) == 2 and list(adapter.pagination_skipped.values()) == ["robots_disallowed"]


def test_later_duplicate_closed_status_cannot_be_lost(tmp_path, project, now):
    class Source:
        name = project.source
        def fetch(self, client, *, now):
            return [project, replace(project, open_status="awarded", status_evidence="Исполнитель определён")]
    result = execute(Settings(), [Source()], object(), StateStore(tmp_path / "state.json"), now=now)
    assert result.checked == 1 and result.eligible == 0
    assert result.candidates[0][0].open_status == "awarded"


def test_conflicting_confirmed_publications_fail_closed(project, now):
    from jobradar.deduplication import deduplicate_projects
    candidates = deduplicate_projects([project, replace(project, published_at=now - timedelta(days=10))])
    assert len(candidates) == 1 and not candidates[0].publication_confirmed
    assert not assess_project(candidates[0], now=now).eligible


def test_pagination_challenge_stops_host_and_does_not_fetch_details(fixture_dir, now):
    adapter = FlRuAdapter()
    adapter.discovery_urls = [adapter.discovery_urls[0]]
    urls = []
    class Public:
        def get_listing(self, url, hosts):
            urls.append(url)
            if "page-2" in url:
                return '<title>CAPTCHA</title>'
            return (fixture_dir / 'fl_ru.html').read_text(encoding='utf-8') + '<a href="page-2/">2</a>'
    projects = adapter.fetch(Public(), now=now)
    assert len(urls) == 2 and len(projects) == 2 and adapter.details_fetched == 0
    assert list(adapter.page_errors.values()) == ["challenge_page"]
    assert not any(p.publication_confirmed for p in projects)


def test_detail_confirmation_is_bounded_and_failure_is_visible(fixture_dir, now):
    adapter = FlRuAdapter()
    adapter.discovery_urls = [adapter.discovery_urls[0]]
    adapter.max_detail_pages = 1
    html = (fixture_dir / "fl_ru.html").read_text(encoding="utf-8").replace("вчера", "1 час назад")
    urls = []
    class Public:
        def get_listing(self, url, hosts):
            urls.append(url)
            if "/category/" in url:
                return html
            raise SourceError("network_error")
    projects = adapter.fetch(Public(), now=now)
    assert len(urls) == 2 and len(adapter.detail_errors) == 1
    assert all(not p.publication_confirmed for p in projects)


def test_pending_dedup_applies_to_cross_source_after_restart(tmp_path, project, now):
    state = StateStore(tmp_path / "state.json")
    state.reserve(project, assess_project(project, now=now), now=now)
    copy = replace(project, source="telegram_public", source_id="test_channel_20", origin_kind="repost",
                   url="https://t.me/test_channel/20", external_url=project.url)
    loaded = StateStore(state.path)
    assert loaded.contains(copy)
    assert not loaded.reserve(copy, assess_project(copy, now=now), now=now)
