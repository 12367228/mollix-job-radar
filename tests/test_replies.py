from dataclasses import replace

import pytest

from jobradar.reply_templates import generate_reply, recommended_price, reply_kind
from jobradar.scoring import score_project


@pytest.mark.parametrize("title,kind,detail", [
    ("Telegram бот", "telegram", "сценарии бота"),
    ("Python скрипт", "python", "входных данных"),
    ("REST API интеграция", "api", "документацию API"),
    ("Парсинг каталога", "parsing", "публичные страницы"),
    ("React форма", "web", "ссылку на сайт"),
    ("Интеграция 1C УНФ", "partial_stack", "платформу"),
    ("n8n automation", "automation", "схему процесса"),
    ("Docker Linux", "infrastructure", "окружения"),
])
def test_personalized_template(project, title, kind, detail):
    p = replace(project, title=title, description=None)
    assert reply_kind(p) == kind
    reply = generate_reply(p)
    assert title in reply and detail in reply
    assert "портфолио" not in reply and "5 лет" not in reply


def test_price_respects_ruble_budget_and_flags_foreign_currency(project, now):
    score = score_project(project, now=now)
    p = replace(project, budget_min=12000, budget_max=15000)
    assert "12 000–15 000 RUB" in recommended_price(p, score)
    foreign = replace(project, budget_min=100, budget_max=200, currency="USD")
    assert "курс не применялся" in recommended_price(foreign, score)
    low = replace(project, budget_min=100, budget_max=200)
    assert "не пересекается" in recommended_price(low, score)
