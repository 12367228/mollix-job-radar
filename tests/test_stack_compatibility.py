from dataclasses import replace
from datetime import timedelta
import json

import pytest

from jobradar.__main__ import main, print_candidate
from jobradar.config import Settings
from jobradar.reply_templates import generate_reply, reply_kind
from jobradar.scoring import assess_project
from jobradar.stack import HIGH_MATCH, PARTIAL_MATCH, LOW_UNKNOWN_MATCH, assess_stack
from jobradar.state import StateStore
from jobradar.telegram import format_message


@pytest.mark.parametrize("title,expected,points", [
    ("Доработать сайт на Ruby on Rails", LOW_UNKNOWN_MATCH, -3),
    ("Виджеты подбора автозапчастей Java + JS", LOW_UNKNOWN_MATCH, -3),
    ("Python Telegram bot на aiogram", HIGH_MATCH, 2),
    ("n8n API automation", HIGH_MATCH, 2),
    ("React bugfix", HIGH_MATCH, 2),
    ("Доработка сайта OpenCart", PARTIAL_MATCH, 0),
])
def test_requested_stack_cases(project, now, title, expected, points):
    p = replace(project, title=title, description=None, category="Программирование")
    a = assess_project(p, now=now)
    assert a.stack_compatibility == expected and a.scored
    assert next(r.points for r in a.reasons if r.code == "stack_compatibility") == points
    assert a.raw_score == sum(r.points for r in a.reasons)
    assert ("STACK_MISMATCH" in a.risk_flags) == (expected == LOW_UNKNOWN_MATCH)
    if expected == LOW_UNKNOWN_MATCH:
        assert a.score == 6 and a.label == "GOOD" and a.complexity >= 6
    elif expected == PARTIAL_MATCH:
        assert a.score == 7 and a.label == "GOOD" and a.complexity < 6
    else:
        assert a.label == "FIRE" and a.complexity < 6


@pytest.mark.parametrize("stack", ["Python", "Telegram Bot API", "aiogram", "FastAPI", "REST API", "webhooks",
    "n8n", "automation", "автоматизация", "автоматизировать", "parsing", "scraping", "парсер", "парсинг",
    "JavaScript", "TypeScript", "React", "Node.js", "Docker", "Linux", "VPS", "SQL", "PostgreSQL"])
def test_high_profile(project, stack):
    assert assess_stack(replace(project, title=stack, description=None)).level == HIGH_MATCH


@pytest.mark.parametrize("stack", ["1C programming", "Программирование 1С", "Доработать конфигурацию 1С",
    "Интеграция с 1C УНФ", "Простой обмен данными с 1С через REST API",
    "PHP", "WordPress", "OpenCart", "payment integrations", "Подключение платежной системы",
    "Google Maps API", "Google APIs", "API Google Sheets", "Bitrix", "Битрикс24"])
def test_partial_platform_requirement_overrides_incidental_api_or_js(project, stack):
    p = replace(project, title=stack, description="Есть REST API, интеграция, JavaScript и автоматизация.")
    assert assess_stack(p).level == PARTIAL_MATCH


@pytest.mark.parametrize("stack", ["Ruby", "Ruby on Rails", "Java", "C#", "C++", "Flutter", "Kotlin", "Swift",
    "Доработать Клеверенс", "Cleverence-specific development", "GetCourse-specific administration",
    "Настроить get course с 0", "Настройка Windows RDP"])
def test_unknown_required_platform_overrides_all_high_signals(project, now, stack):
    p = replace(project, title=stack, description="Python, API, React, n8n, JavaScript: всё это тоже используется.", category="Программирование")
    a = assess_project(p, now=now)
    assert a.stack_compatibility == LOW_UNKNOWN_MATCH and "STACK_MISMATCH" in a.risk_flags
    assert a.score <= 6 and a.label != "FIRE" and a.complexity >= 6


@pytest.mark.parametrize("title", ["JavaScript React", "TypeScript", "Node.js", "React JS"])
def test_java_is_not_a_substring_of_javascript(project, now, title):
    a = assess_project(replace(project, title=title, description=None, category="Программирование"), now=now)
    assert a.stack_compatibility == HIGH_MATCH and "STACK_MISMATCH" not in a.risk_flags


@pytest.mark.parametrize("text", ["Стек: React, Phoenix", "Стек: React+Phoenix", "Backend на Haskell",
    "Обязательный stack: Python, Erlang", "Стек: TypeScript и C++17",
    "Требуется Phoenix. Есть React и REST API.", "Нужен разработчик Erlang. Интерфейс на React."])
def test_explicit_unlisted_technology_is_not_hidden_by_high_match(project, now, text):
    p = replace(project, title="Доработка сайта", description=text)
    a = assess_project(p, now=now)
    assert a.stack_compatibility == LOW_UNKNOWN_MATCH and a.complexity >= 6 and a.score <= 6
    assert "Работаю с" not in generate_reply(p)


def test_1c_declared_stack_is_partial_not_unknown_token_c(project):
    p = replace(project, title="Доработать расширение конфигурации", description="Стек: 1C, REST API")
    assert assess_stack(p).level == PARTIAL_MATCH


@pytest.mark.parametrize("description", ["Java не требуется. Нужен React.", "React без Java", "React. Ruby не используется."])
def test_explicitly_excluded_technology_is_not_required(project, description):
    p = replace(project, title="Доработать сайт", description=description)
    assert assess_stack(p).level == HIGH_MATCH


def test_no_experience_requirement_does_not_remove_language_requirement(project):
    p = replace(project, title="Доработать сайт", description="React. Опыт Java не требуется.")
    assert assess_stack(p).level == LOW_UNKNOWN_MATCH


def test_category_and_generic_bugfix_do_not_invent_stack(project, now):
    p = replace(project, title="Исправить ошибку на сайте", description=None, category="Программирование")
    a = assess_project(p, now=now)
    assert a.stack_compatibility == LOW_UNKNOWN_MATCH and a.complexity >= 6
    assert "STACK_MISMATCH" in a.risk_flags and a.score <= 6 and a.label != "FIRE"
    assert reply_kind(p) == "clarify_stack"


@pytest.mark.parametrize("title", ["Доработать сайт Ruby on Rails", "Java + JS API", "Flutter API", "C# backend"])
def test_mismatch_reply_never_claims_unverified_or_unrelated_experience(project, now, title):
    p = replace(project, title=title, description="Нужно исправить ошибку и API.")
    reply = generate_reply(p)
    assert "Готов посмотреть текущий проект и оценить объём" in reply
    assert "уточнить используемый стек и характер доработок" in reply
    assert all(text not in reply for text in ("Работаю с", "JavaScript, React", "опыт Java", "опыт Ruby", "Могу приступить"))
    message = format_message(p, assess_project(p, now=now))
    assert "STACK_COMPATIBILITY: LOW_UNKNOWN_MATCH" in message and "STACK_MISMATCH" in message
    assert "Работаю с" not in message and "🔥 БРАТЬ" not in message


def test_partial_reply_does_not_use_generic_react_or_python_fallback(project):
    p = replace(project, title="Доработать OpenCart", description="REST API и JS")
    assert reply_kind(p) == "partial_stack"
    reply = generate_reply(p)
    assert "платформу" in reply and "Работаю с" not in reply


def test_stack_bonus_applies_once_and_caps_never_override_hard_gates(project, now):
    a = assess_project(project, now=now)
    b = assess_project(replace(project, title=project.title + " Python React n8n API" * 30), now=now)
    for score in (a, b):
        reasons = [r for r in score.reasons if r.code == "stack_compatibility"]
        assert len(reasons) == 1 and reasons[0].points == 2
    for changes in ({"published_at": now - timedelta(hours=25)}, {"publication_confirmed": False},
                    {"open_status": "closed"}, {"budget_min": 1000, "budget_max": 1000}):
        for title in ("Python API", "Ruby API"):
            score = assess_project(replace(project, title=title, **changes), now=now)
            assert score.score <= 5 and score.label == "SKIP"


def test_stack_is_present_even_when_relevance_rejects_and_scoring_does_not_run(project, now):
    p = replace(project, title="Написать отзывы о Java", description=None, category=None)
    a = assess_project(p, now=now)
    assert not a.scored and not a.relevance_gate and a.stack_compatibility == LOW_UNKNOWN_MATCH
    assert a.score == 0 and a.label == "SKIP" and a.complexity >= 6


def test_stack_fields_are_visible_in_cli_json_and_persisted_assessment(project, now, tmp_path, capsys):
    p = replace(project, title="Доработать Ruby on Rails", description=None, category="Программирование")
    a = assess_project(p, now=now)
    print_candidate(p, a, False)
    text = capsys.readouterr().out
    assert "STACK_COMPATIBILITY: LOW_UNKNOWN_MATCH" in text and "COMPLEXITY: 6/10" in text
    assert "STACK_DETAILS: Ruby / Ruby on Rails" in text
    state = StateStore(tmp_path / "state.json")
    state.reserve(p, a, now=now)
    stored = StateStore(state.path).entries[p.key]["assessment"]
    assert stored["stack_compatibility"] == LOW_UNKNOWN_MATCH and stored["stack_reason"]


def test_real_cli_path_emits_stack_json_without_network_or_state_writes(tmp_path, fixture_dir, monkeypatch, capsys, now):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("jobradar.__main__.Settings.from_env", lambda: Settings(state_path=tmp_path / "state.json"))
    def forbidden(*a, **kw):
        pytest.fail("No Telegram, HTTP or state writes allowed in fixture check")
    monkeypatch.setattr("jobradar.__main__.TelegramClient", forbidden)
    monkeypatch.setattr("jobradar.__main__.PublicHTTPClient", forbidden)
    monkeypatch.setattr(StateStore, "save", forbidden)
    assert main(["check", "--top", "--json", "--fixtures", str(fixture_dir), "--now", now.isoformat()]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["projects"] and data["summary"]["sent"] == 0
    assert all(p["assessment"]["stack_compatibility"] in {HIGH_MATCH, PARTIAL_MATCH, LOW_UNKNOWN_MATCH} for p in data["projects"])
    assert not (tmp_path / "state.json").exists()
