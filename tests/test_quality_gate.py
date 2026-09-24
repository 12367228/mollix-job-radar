from dataclasses import replace
from datetime import timedelta
import json

import pytest

from jobradar.__main__ import check_candidates, main
from jobradar.config import ConfigError, Settings
from jobradar.delivery import apply_delivery_gate
from jobradar.reply_templates import generate_reply
from jobradar.runner import execute
from jobradar.scoring import assess_project
from jobradar.stack import HIGH_MATCH, PARTIAL_MATCH, LOW_UNKNOWN_MATCH
from jobradar.state import StateStore


@pytest.mark.parametrize("changes,level,label,score,deliverable,flag", [
    ({"responses_count": 2}, HIGH_MATCH, "FIRE", 10, True, None),
    ({"responses_count": 29}, HIGH_MATCH, "GOOD", 7, False, "HIGH_COMPETITION"),
    ({"responses_count": 55}, HIGH_MATCH, "GOOD", 6, False, "HIGH_COMPETITION"),
    ({"title": "Доработать Tilda через API"}, LOW_UNKNOWN_MATCH, "GOOD", 6, False, "STACK_MISMATCH"),
    ({"title": "Разработать Android приложение с API"}, LOW_UNKNOWN_MATCH, "GOOD", 6, False, "STACK_MISMATCH"),
    ({"title": "Доработать OpenCart"}, PARTIAL_MATCH, "GOOD", 7, True, None),
    ({"open_status": "unknown", "status_evidence": None}, HIGH_MATCH, "GOOD", 7, True, "OPEN_STATUS_UNKNOWN"),
    ({"title": "Перенос полноценной конфигурации 1С ERP"}, PARTIAL_MATCH, "GOOD", 6, False, "DEEP_1C"),
    ({"responses_count": 5}, HIGH_MATCH, "FIRE", 10, True, None),
])
def test_requested_quality_cases(project, now, changes, level, label, score, deliverable, flag):
    p = replace(project, **changes)
    a = assess_project(p, now=now)
    assert (a.stack_compatibility, a.label, a.score) == (level, label, score)
    assert a.telegram_deliverable is deliverable
    assert a.to_dict()["telegram_deliverable"] is deliverable
    if flag:
        assert flag in a.risk_flags
    assert a.raw_score == sum(r.points for r in a.reasons)


@pytest.mark.parametrize("responses,cap,deliverable", [
    (0, 10, True), (3, 10, True), (4, 10, True), (7, 10, True),
    (8, 10, True), (15, 10, True), (16, 10, True), (20, 10, True),
    (21, 7, False), (30, 7, False), (31, 6, False), (50, 6, False), (51, 6, False),
    (None, 7, False),
])
def test_competition_caps_and_default_delivery_boundaries(project, now, responses, cap, deliverable):
    a = assess_project(replace(project, responses_count=responses), now=now)
    assert a.score == cap and a.telegram_deliverable is deliverable
    if responses is None:
        assert "RESPONSES_UNKNOWN" in a.delivery_blockers


@pytest.mark.parametrize("status,evidence", [("unknown", None), ("open", None), ("open", "  "),
                                            ("unknown", "no closed label found")])
def test_fire_requires_both_open_status_and_source_evidence(project, now, status, evidence):
    a = assess_project(replace(project, open_status=status, status_evidence=evidence), now=now)
    assert a.label == "GOOD" and a.score == 7 and "OPEN_STATUS_UNKNOWN" in a.risk_flags


@pytest.mark.parametrize("title", ["Интеграция 1С через REST API", "Простой обмен данными с 1C",
    "Простое расширение 1С", "Simple extension for 1C", "Программирование 1С"])
def test_all_1c_tasks_are_at_most_partial_and_replies_make_no_experience_claim(project, now, title):
    p = replace(project, title=title)
    a = assess_project(p, now=now)
    assert a.stack_compatibility == PARTIAL_MATCH and a.score == 7 and a.label == "GOOD"
    assert "DEEP_1C" not in a.risk_flags
    assert "Работаю с" not in generate_reply(p) and "опыт" not in generate_reply(p).lower()


@pytest.mark.parametrize("title", ["Перенос полноценной конфигурации 1С ERP", "Перенести всю конфигурацию 1C",
    "Перенести конфигурацию и базу 1С ERP Управление предприятием 2 (1С Предприятие 8.3.27)",
    "Перенос базы и конфигурации 1С ERP", "Миграция конфигурации 1С ERP", "1C configuration migration",
    "Архитектура конфигурации 1С", "Сложные ERP-модули 1С", "Крупное внедрение 1С",
    "Комплексное внедрение 1С ERP", "1C full configuration migration", "1C complex ERP modules"])
def test_deep_1c_cannot_escape_cap_through_api_or_freshness_bonus(project, now, title):
    p = replace(project, title=title)
    a = assess_project(p, now=now)
    assert "DEEP_1C" in a.risk_flags and a.score <= 6 and a.complexity >= 6
    assert not a.telegram_deliverable and "Работаю с" not in generate_reply(p)


def test_live_1c_erp_configuration_migration_is_watchlist_only(project, now):
    p = replace(project, source_id="11182", url="https://freelance.ru/task/view/11182",
                title="Перенести конфигурацию и базу 1С ERP Управление предприятием 2 (1С Предприятие 8.3.27)",
                description=None, budget_min=None, budget_max=None, responses_count=10,
                published_at=now - timedelta(hours=1.4), open_status="unknown", status_evidence=None)
    a = assess_project(p, now=now)
    assert a.label == "GOOD" and a.score == 6 and a.complexity >= 6
    assert "DEEP_1C" in a.risk_flags and "MIN_SCORE" in a.delivery_blockers
    assert not a.telegram_deliverable


@pytest.mark.parametrize("title", ["Tilda", "Тильда", "Android", "Андроид", "Ruby on Rails", "Java + JS"])
def test_mismatch_requires_two_explicit_config_relaxations(project, now, title):
    p = replace(project, title="Доработать " + title)
    a = assess_project(p, now=now)
    assert a.stack_compatibility == LOW_UNKNOWN_MATCH and not a.telegram_deliverable
    assert not apply_delivery_gate(p, a, min_score=6).telegram_deliverable
    assert not apply_delivery_gate(p, a, allow_stack_mismatch=True).telegram_deliverable
    assert apply_delivery_gate(p, a, min_score=6, allow_stack_mismatch=True).telegram_deliverable


@pytest.mark.parametrize("low,high,currency,title,exception", [
    (50000, 50000, "RUB", "Python bot", True), (None, 50000, "RUB", "Python bot", True),
    (50000, None, "RUB", "Python bot", True), (20000, 60000, "RUB", "Python bot", False),
    (49999, 49999, "RUB", "Python bot", False), (50000, 50000, "USD", "Python bot", False),
    (50000, 50000, None, "Python bot", False), (None, None, "RUB", "Python bot", False),
    (50000, 50000, "RUB", "OpenCart API", False), (50000, 50000, "RUB", "Ruby API", False),
])
def test_over_fifty_budget_exception_does_not_bypass_other_gates(project, now, low, high, currency, title, exception):
    p = replace(project, title=title, responses_count=55, budget_min=low, budget_max=high, currency=currency)
    a = assess_project(p, now=now)
    assert not a.telegram_deliverable and {"MIN_SCORE", "COMPETITION"} <= set(a.delivery_blockers)
    assert ("COMPETITION_OVER_50" not in a.delivery_blockers) == exception
    relaxed = apply_delivery_gate(p, a, min_score=6, max_responses=100)
    assert relaxed.telegram_deliverable is exception


@pytest.mark.parametrize("changes", [{"published_at": None}, {"publication_confirmed": False},
    {"publication_evidence": None}, {"url": "https://freelance.ru/task"}, {"open_status": "closed"},
    {"open_status": "awarded"}, {"open_status": "not_accepting"}, {"budget_min": 1000, "budget_max": 1000}])
def test_relaxed_quality_settings_never_bypass_hard_safety(project, now, changes):
    p = replace(project, **changes)
    a = apply_delivery_gate(p, assess_project(p, now=now), min_score=6, max_responses=10000, allow_stack_mismatch=True)
    assert not a.telegram_deliverable and a.label == "SKIP"


def test_caps_are_independent_and_never_raise_a_low_score(project, now):
    p = replace(project, title="Перенос полноценной конфигурации 1С ERP с Ruby",
                published_at=now - timedelta(hours=20), responses_count=55,
                budget_min=3000, budget_max=3000, open_status="unknown", status_evidence=None)
    a = assess_project(p, now=now)
    assert {"DEEP_1C", "STACK_MISMATCH", "HIGH_COMPETITION", "OPEN_STATUS_UNKNOWN", "OLDER_THAN_HOT_WINDOW"} <= set(a.risk_flags)
    assert a.score < 6 and a.label == "SKIP" and not a.telegram_deliverable


class StaticSource:
    name = "freelance_ru"

    def __init__(self, projects):
        self.projects = projects

    def fetch(self, client, *, now):
        return self.projects


class FakeSender:
    def __init__(self):
        self.messages = []

    def send_message(self, message):
        self.messages.append(message)


def jobs_for_quality_check(project):
    changes = [{}, {"responses_count": 29}, {"responses_count": 55}, {"title": "Доработать Tilda API"},
               {"title": "Доработать OpenCart API"}, {"open_status": "unknown", "status_evidence": None},
               {"title": "Архитектура конфигурации 1С ERP"}]
    return [replace(project, source_id=str(i), url=f"https://freelance.ru/task/view/{i}", **c)
            for i, c in enumerate(changes, start=1000)]


def test_runner_quality_counters_and_read_only_check(project, now, tmp_path, monkeypatch):
    projects = jobs_for_quality_check(project)
    state = StateStore(tmp_path / "state.json")
    state.reserve(projects[0], assess_project(projects[0], now=now), now=now)
    state.mark_sent(projects[0], now=now)
    before = state.path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("Check must not mutate state")
    monkeypatch.setattr(state, "save", forbidden)
    result = execute(Settings(), [StaticSource(projects)], object(), state, now=now)
    summary = result.summary()
    assert {k: summary[k] for k in ("fetched", "fresh", "eligible", "FIRE", "GOOD", "telegram_deliverable",
            "filtered_by_competition", "filtered_by_stack", "filtered_by_open_status")} == {
        "fetched": 7, "fresh": 7, "eligible": 7, "FIRE": 1, "GOOD": 6, "telegram_deliverable": 2,
        "filtered_by_competition": 2, "filtered_by_stack": 1, "filtered_by_open_status": 1}
    assert state.path.read_bytes() == before
    assert not next(a for p, a, _ in result.candidates if p == projects[0]).telegram_deliverable
    assert len(list(check_candidates(result, watchlist=True))) == 3


def test_runner_does_not_reserve_or_send_filtered_projects(project, now, tmp_path, monkeypatch):
    projects = jobs_for_quality_check(project)
    state = StateStore(tmp_path / "state.json")
    sender = FakeSender()
    reserved = []
    original = state.reserve
    def reserve(p, a, **kwargs):
        assert a.telegram_deliverable
        reserved.append(p.source_id)
        return original(p, a, **kwargs)
    monkeypatch.setattr(state, "reserve", reserve)
    result = execute(Settings(), [StaticSource(projects)], object(), state, telegram=sender, now=now, sleep=lambda _: None)
    assert result.sent == len(sender.messages) == 3
    assert set(reserved) == {"1000", "1004", "1005"}
    for p in (projects[1], projects[2], projects[3], projects[6]):
        assert not StateStore(state.path).contains(p)


def test_runner_uses_configured_threshold_and_rechecks_quality_before_send(project, now, tmp_path):
    # Discovery admits FIRE >=8. On the second clock tick it ages into GOOD 7,
    # still eligible, but no longer deliverable under the configured minimum 8.
    p = replace(project, published_at=now - timedelta(hours=5, minutes=59))
    ticks = iter([now, now, now + timedelta(minutes=2)])
    sender = FakeSender()
    state = StateStore(tmp_path / "state.json")
    result = execute(Settings(telegram_min_score=8), [StaticSource([p])], object(), state,
                     telegram=sender, clock=lambda: next(ticks), sleep=lambda _: None)
    assert result.sent == 0 and not sender.messages and not state.contains(p)


@pytest.mark.parametrize("flags", [["--watchlist"], ["--watchlist", "--json"], ["--verbose"]])
def test_watchlist_and_verbose_cli_are_read_only_and_explain_delivery(project, now, tmp_path, monkeypatch, capsys, flags):
    monkeypatch.chdir(tmp_path)
    settings = Settings(state_path=tmp_path / "state.json")
    projects = jobs_for_quality_check(replace(project, published_at=None))
    # Use fixtures clock while providing a static parser: no HTTP client is constructed.
    class Source(StaticSource):
        def parse(self, html, *, now):
            return jobs_for_quality_check(replace(project, published_at=now))
    (tmp_path / "freelance_ru.html").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr("jobradar.__main__.Settings.from_env", lambda: settings)
    monkeypatch.setattr("jobradar.__main__.configured_sources", lambda _: [Source(projects)])
    def forbidden(*args, **kwargs):
        pytest.fail("No Telegram, network or state writes allowed")
    monkeypatch.setattr("jobradar.__main__.TelegramClient", forbidden)
    monkeypatch.setattr("jobradar.__main__.PublicHTTPClient", forbidden)
    monkeypatch.setattr(StateStore, "save", forbidden)
    assert main(["check", *flags, "--fixtures", str(tmp_path), "--now", now.isoformat()]) == 0
    output = capsys.readouterr().out
    if "--json" in flags:
        data = json.loads(output)
        assert len(data["projects"]) == 3
        assert all(not p["assessment"]["telegram_deliverable"] for p in data["projects"])
        assert all(p["assessment"]["delivery_blockers"] for p in data["projects"])
    else:
        assert "STACK_MISMATCH" in output and "TELEGRAM_DELIVERABLE: false" in output
        assert "DELIVERY_BLOCKERS:" in output
        assert output.count("SOURCE_ID:") == (7 if "--verbose" in flags else 3)
    assert not settings.state_path.exists()


def test_quality_configuration_defaults_dotenv_and_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    keys = ("TELEGRAM_MIN_SCORE", "TELEGRAM_MAX_RESPONSES", "TELEGRAM_ALLOW_STACK_MISMATCH")
    for key in keys:
        monkeypatch.setenv(key, "temporary")
        monkeypatch.delenv(key)
    s = Settings.from_env()
    assert (s.telegram_min_score, s.telegram_max_responses, s.telegram_allow_stack_mismatch) == (7, 20, False)
    (tmp_path / ".env").write_text("TELEGRAM_MIN_SCORE=6\nTELEGRAM_MAX_RESPONSES=60\nTELEGRAM_ALLOW_STACK_MISMATCH=true\n", encoding="utf-8")
    s = Settings.from_env()
    assert (s.telegram_min_score, s.telegram_max_responses, s.telegram_allow_stack_mismatch) == (6, 60, True)
    monkeypatch.setenv("TELEGRAM_MIN_SCORE", "8")
    assert Settings.from_env().telegram_min_score == 8


@pytest.mark.parametrize("key,value", [("TELEGRAM_MIN_SCORE", "5"), ("TELEGRAM_MIN_SCORE", "11"),
    ("TELEGRAM_MIN_SCORE", "nan"), ("TELEGRAM_MAX_RESPONSES", "-1"), ("TELEGRAM_MAX_RESPONSES", "10001"),
    ("TELEGRAM_MAX_RESPONSES", "2.5"), ("TELEGRAM_ALLOW_STACK_MISMATCH", "yes")])
def test_invalid_quality_config_fails_safely(tmp_path, monkeypatch, key, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError, match=key):
        Settings.from_env()
