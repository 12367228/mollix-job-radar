from dataclasses import replace
from datetime import timedelta

import pytest

from jobradar.filtering import NEGATIVE_KEYWORDS, POSITIVE_KEYWORDS, contains, match_keywords
from jobradar.scoring import score_project


@pytest.mark.parametrize("keyword", POSITIVE_KEYWORDS)
def test_positive_keywords(keyword):
    assert contains("Задача: " + keyword.upper(), keyword)


@pytest.mark.parametrize("keyword", NEGATIVE_KEYWORDS)
def test_negative_keywords(keyword):
    assert contains("Нужно: " + keyword, keyword)


def test_keyword_word_boundaries_and_inflections(project):
    assert not contains("работа", "бот")
    assert not contains("capital", "api")
    assert not contains("debugger", "bug")
    assert contains("для ботов", "бот")
    assert contains("Интеграции 1C:УНФ", "1с")
    assert contains("автоматизировать", "автоматизация")


def test_good_project_is_fire_with_transparent_score(project, now):
    assessment = score_project(project, now=now)
    assert 8 <= assessment.score <= 10
    assert assessment.label == "FIRE" and assessment.eligible
    assert assessment.raw_score == sum(r.points for r in assessment.reasons)
    assert {"specialization", "fresh", "budget", "competition", "existing", "short_task"} <= {r.code for r in assessment.reasons}


def test_single_negative_is_not_a_blacklist(project, now):
    p = replace(project, description=project.description + " Дизайн не менять.")
    result = score_project(p, now=now)
    assert result.eligible
    assert "дизайн" in result.negative_keywords
    assert "negative_topics" not in {r.code for r in result.reasons}


def test_telegram_as_contact_is_not_a_programming_job(project, now):
    p = replace(project, title="Оператор чата Telegram", description="Общаться с клиентами и отвечать на вопросы. Пишите в телеграм.")
    result = score_project(p, now=now)
    assert result.score <= 5 and not result.eligible


def test_explicitly_no_programming_does_not_count_as_programming(project, now):
    p = replace(project, title="Оператор Telegram", description="Писать в чате и заполнять анкеты; программирование не требуется.")
    assert not score_project(p, now=now).eligible


def test_non_programming_negative_job_skipped(project, now):
    p = replace(project, title="Логотип и дизайн", description="Тексты для SMM, копирайтинг, продажи.")
    assert not score_project(p, now=now).eligible


@pytest.mark.parametrize("description,code", [
    ("Обязателен Senior, Python и REST API.", "experience_gate"),
    ("Python: 5+ years mandatory.", "experience_gate"),
    ("Обязательны подтвержденные кейсы и портфолио.", "experience_gate"),
    ("Создать marketplace from scratch и mobile app.", "large_project"),
    ("Настроить Python API, срок: 30 дней.", "large_project"),
])
def test_penalties(project, now, description, code):
    result = score_project(replace(project, description=description), now=now)
    assert next(r.points for r in result.reasons if r.code == code) < 0


def test_budget_and_competition_penalties(project, now):
    result = score_project(replace(project, budget_min=1000, budget_max=2000, responses_count=40), now=now)
    assert {"budget_too_low", "competition"} <= {r.code for r in result.reasons}
    assert result.label == "SKIP" and not result.eligible
    foreign = score_project(replace(project, budget_min=100, budget_max=200, currency="USD"), now=now)
    assert "budget_too_low" not in {r.code for r in foreign.reasons}


def test_missing_metadata_does_not_get_free_bonuses(project, now):
    result = score_project(replace(project, published_at=None, budget_min=None, budget_max=None, responses_count=None), now=now)
    assert not {"fresh", "budget", "competition"} & {r.code for r in result.reasons}


def test_old_project_never_sent_even_with_maximum_match(project, now):
    result = score_project(replace(project, published_at=now - timedelta(days=8)), now=now)
    assert result.score <= 5 and not result.eligible


def test_future_date_gets_no_freshness_bonus(project, now):
    result = score_project(replace(project, published_at=now + timedelta(days=1)), now=now)
    assert "fresh" not in {r.code for r in result.reasons}


def test_good_threshold_and_keyword_stuffing(project, now):
    p = replace(project, title="Python", description=None, published_at=now - timedelta(hours=20), budget_min=5000, budget_max=5000, responses_count=None)
    assert score_project(p, now=now).score == 7  # +2 stack fit, then the >6h ceiling.
    assert score_project(p, now=now).label == "GOOD"
    assert score_project(replace(p, title="Python " * 50), now=now).score == 7
