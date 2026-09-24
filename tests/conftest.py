from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from jobradar.models import Project

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def now():
    return datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


@pytest.fixture
def project(now):
    return Project(source="freelance_ru", source_id="123", title="Доработать Telegram-бота на Python",
                   description="Нужно добавить выгрузку заявок в CSV через REST API в существующий бот. Код и пример входных данных готовы. Срок: 3 дня.",
                   url="https://freelance.ru/task/view/123", published_at=now,
                   publication_confirmed=True, publication_evidence="fixture: source publication field", open_status="open",
                   status_evidence="fixture: enabled reply button",
                   budget_min=Decimal(10000), budget_max=Decimal(20000), currency="RUB", responses_count=2)


@pytest.fixture
def fixture_dir():
    return FIXTURES
