from dataclasses import replace
import json
from pathlib import Path

import pytest

from jobradar.filtering import POSITIVE_KEYWORDS, relevance_gate
from jobradar.scoring import assess_project

CASES = json.loads((Path(__file__).parent / "fixtures/relevance_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[str(i + 1) for i in range(len(CASES))])
def test_requested_examples(case, project, now):
    p = replace(project, title=case["title"], description=None, category=None)
    if "budget" in case:
        p = replace(p, budget_min=case["budget"], budget_max=case["budget"])
    assessment = assess_project(p, now=now)
    assert assessment.relevance_gate == case["pass"]
    assert assessment.scored == case["pass"]
    if "risk" in case:
        assert case["risk"] in assessment.risk_flags
        assert assessment.label != "FIRE" and assessment.score <= 7
        assert any(r.points < 0 for r in assessment.reasons)
    if "decision" in case:
        assert assessment.label == case["decision"] and not assessment.eligible


@pytest.mark.parametrize("title", [
    "Копирайтинг про Python и API", "Написать отзывы о Telegram-боте", "Заполнить 500 карточек товара вручную",
    "Заполнение анкет", "PowerPoint: создать презентации курса Python", "Ретушь фотографий в Photoshop",
    "Перевести документацию Django", "Контент-менеджмент сайта", "Ручное наполнение сайта",
    "Продажи софта Python", "Поиск клиентов", "Холодные звонки", "Дизайн сайта без разработки",
    "Excel: составить прайс вручную", "Google Docs: ведение реестра", "Оператор маркетплейса",
    "Telegram: рассылка контента, гибкий график", "Исправить ошибки в презентации",
])
def test_negative_primary_task_overrides_incidental_keywords_and_category(project, title):
    p = replace(project, title=title, description="Работать руками. В нашей компании есть Python API.",
                category="Веб-разработка и IT")
    assert not relevance_gate(p).passed


@pytest.mark.parametrize("title", [
    "Автоматизировать заполнение карточек через API", "Написать Python скрипт для Excel",
    "Разработать Telegram-бота для продаж", "Настроить n8n для Google Docs",
    "Создать API перевода", "Парсинг карточек товаров через API", "Доработать aiogram Telegram bot",
])
def test_technical_workflow_automation_is_not_blacklisted(project, title):
    assert relevance_gate(replace(project, title=title, description=None, category=None)).passed


@pytest.mark.parametrize("title", ["Разработка и адаптивная верстка сайта по готовым прототипам",
                                   "Исполнитель на разработку программного комплекса"])
def test_technical_category_and_development_intent_override_incidental_topics(project, title):
    p = replace(project, title=title, description="Дизайн готов. Решение для отдела продаж, включает отзывы клиентов.",
                category="Веб-разработка и IT")
    assert relevance_gate(p).passed


@pytest.mark.parametrize("title,description", [
    ("Карточки товаров", "Нужно автоматизировать их заполнение через API."),
    ("PowerPoint", "Написать Python скрипт для генерации презентаций из JSON."),
])
def test_automation_intent_can_be_in_description(project, title, description):
    assert relevance_gate(replace(project, title=title, description=description, category=None)).passed


@pytest.mark.parametrize("category", [
    "Программирование", "Веб-разработка и IT", "Web development", "Chatbot development",
    "Automation", "API", "Python", "1C", "DevOps", "Programming", "Разработка чат-ботов",
])
def test_category_can_supply_technical_relevance(project, category):
    assert relevance_gate(replace(project, title="Нужна небольшая доработка", description=None, category=category)).passed


@pytest.mark.parametrize("keyword", POSITIVE_KEYWORDS)
def test_strong_keyword_passes_without_category(project, keyword):
    assert relevance_gate(replace(project, title=keyword, description=None, category=None)).passed


def test_rejected_job_never_reaches_scoring(project, now, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Scoring must not run for rejected projects")
    monkeypatch.setattr("jobradar.scoring.score_project", forbidden)
    a = assess_project(replace(project, title="Telegram для связи", description=None, category=None), now=now)
    assert not a.scored and not a.relevance_gate and a.label == "SKIP"
    assert not any(r.points for r in a.reasons)
