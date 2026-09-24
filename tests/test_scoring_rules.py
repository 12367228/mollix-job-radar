from dataclasses import replace
from datetime import timedelta

import pytest

from jobradar.scoring import assess_project


@pytest.mark.parametrize("hours,points,skip", [
    (0, 3, False), (2, 3, False), (2.001, 2, False), (6, 2, False),
    (6.001, 1, False), (12, 1, False), (12.001, 0, False), (24, 0, False),
    (24.001, 0, True), (48, 0, True), (-0.1, 0, False), (None, 0, False),
])
def test_freshness_boundaries(project, now, hours, points, skip):
    a = assess_project(replace(project, published_at=None if hours is None else now - timedelta(hours=hours)), now=now)
    assert sum(r.points for r in a.reasons if r.code == "fresh") == points
    assert ("stale" in {r.code for r in a.reasons}) == skip
    if skip:
        assert a.label == "SKIP" and not a.eligible and a.score <= 5


def test_age_settings_are_used_and_hot_is_separate(project, now):
    p = replace(project, published_at=now - timedelta(hours=7))
    assert assess_project(p, now=now, max_project_age_hours=6).label == "SKIP"
    assert not assess_project(p, now=now).hot
    assert not assess_project(p, now=now, hot_project_age_hours=8).hot  # Hard six-hour ceiling.
    assert assess_project(p, now=now).score == assess_project(p, now=now, hot_project_age_hours=8).score


@pytest.mark.parametrize("budget,points,skip,large", [
    (0, 0, True, False), (1000, 0, True, False), (2999, 0, True, False),
    (3000, -2, False, False), (4999, -2, False, False), (5000, 2, False, False),
    (30000, 2, False, False), (30001, 1, False, False), (100000, 1, False, False),
    (100001, 0, False, True), (None, 0, False, False),
])
def test_budget_boundaries(project, now, budget, points, skip, large):
    a = assess_project(replace(project, budget_min=budget, budget_max=budget), now=now)
    assert sum(r.points for r in a.reasons if r.code == "budget") == points
    assert ("LARGE_PROJECT" in a.risk_flags) == large
    assert (a.label == "SKIP") == skip
    if large:
        assert a.score <= 7


@pytest.mark.parametrize("low,high,currency,skip", [
    (1000, 6000, "RUB", False), (None, 2000, "RUB", True), (2000, None, "RUB", True),
    (1000, 1000, "USD", False), (1000, 1000, None, False),
])
def test_budget_ranges_and_unknown_currency(project, now, low, high, currency, skip):
    a = assess_project(replace(project, budget_min=low, budget_max=high, currency=currency), now=now)
    assert (a.label == "SKIP") == skip


@pytest.mark.parametrize("responses,points", [
    (0, 3), (3, 3), (4, 2), (7, 2), (8, 1), (15, 1), (16, -1), (20, -1),
    (21, -3), (30, -3), (31, -5), (50, -5), (51, -5), (100, -5), (None, 0),
])
def test_competition_and_no_fire_over_thirty(project, now, responses, points):
    a = assess_project(replace(project, responses_count=responses), now=now)
    assert sum(r.points for r in a.reasons if r.code == "competition") == points
    if responses is not None and responses > 30:
        assert a.label != "FIRE" and a.score <= 6


@pytest.mark.parametrize("description,flag", [
    ("Создать marketplace from scratch", "LARGE_PROJECT"),
    ("Разработать mobile app from scratch", "LARGE_PROJECT"),
    ("SaaS platform from scratch", "LARGE_PROJECT"),
    ("CRM с нуля", "LARGE_PROJECT"), ("ERP с нуля", "LARGE_PROJECT"),
    ("Срок: 30 дней", "LARGE_PROJECT"), ("Срок: 20–40 дней", "LARGE_PROJECT"),
    ("enterprise implementation", "LARGE_PROJECT"),
    ("Требуется 3+ года", "EXPERIENCE_GATE"), ("Опыт: От 3 лет", "EXPERIENCE_GATE"),
    ("Обязательные кейсы", "EXPERIENCE_GATE"), ("Подтверждённые внедрения", "EXPERIENCE_GATE"),
    ("Рекомендации клиентов", "EXPERIENCE_GATE"), ("Senior only", "EXPERIENCE_GATE"),
])
def test_risks_reduce_score_and_block_fire(project, now, description, flag):
    baseline = assess_project(replace(project, description=None), now=now)
    a = assess_project(replace(project, description=description), now=now)
    assert flag in a.risk_flags
    assert a.score < baseline.score and a.label != "FIRE"
    assert any(r.points < 0 and flag in r.text for r in a.reasons)


@pytest.mark.parametrize("description", ["Опыт: 1–3 года", "Кейсы не обязательны. Портфолио не требуется.",
                                        "Опыт не требуется.", "Доработать готовую CRM, срок: 5 дней"])
def test_no_risk_for_junior_optional_experience_or_small_existing_product(project, now, description):
    assert not assess_project(replace(project, description=description), now=now).risk_flags


@pytest.mark.parametrize("hours,expected", [(0, "FIRE"), (5, "FIRE"), (8, "GOOD"), (20, "GOOD")])
def test_decision_thresholds(project, now, hours, expected):
    p = replace(project, title="Python", description=None, responses_count=2,
                published_at=now - timedelta(hours=hours), budget_min=5000, budget_max=5000)
    assert assess_project(p, now=now).label == expected
