"""Deterministic drafts: no LLM, portfolio claims or automatic replies to clients."""

from .filtering import match_keywords
from .models import Project
from .scoring import Assessment
from .stack import PARTIAL_MATCH, assess_stack

TEMPLATES = {
    "telegram": ("Telegram-ботами, Python и API-интеграциями", "сценарии бота и описание текущего проекта"),
    "python": ("Python, скриптами и обработкой данных", "пример входных данных, ожидаемый результат и текущий код, если он есть"),
    "api": ("REST API и интеграциями сервисов", "документацию API, пример запроса и описание нужного обмена данными"),
    "parsing": ("Python и сбором данных из публичных источников", "ссылки на публичные страницы и пример нужной таблицы"),
    "web": ("JavaScript, React и доработкой небольших веб-приложений", "ссылку на сайт, шаги воспроизведения проблемы и описание ожидаемого результата"),
    "automation": ("n8n, Python и автоматизацией бизнес-процессов", "схему процесса, список сервисов и пример результата"),
    "infrastructure": ("Docker, Linux и настройкой небольших серверных приложений", "описание окружения и ошибки без паролей и токенов"),
    "sql": ("SQL и PostgreSQL", "схему таблиц без персональных данных, текущий запрос и ожидаемый результат"),
}


def reply_kind(project: Project) -> str:
    stack = assess_stack(project)
    if stack.mismatch:
        return "clarify_stack"
    if stack.level == PARTIAL_MATCH:
        return "partial_stack"
    keywords = set(match_keywords(project).positive)
    for name, words in [
        ("telegram", {"telegram bot", "telegram-бот", "бот", "chatbot", "aiogram"}),
        ("parsing", {"парсер", "парсинг", "scraping"}), ("automation", {"n8n", "automation", "автоматизация"}),
        ("api", {"api", "rest api", "интеграция", "fastapi", "webhook"}),
        ("web", {"react", "javascript", "typescript", "node.js", "доработать сайт", "доработка сайта", "bug", "ошибка", "frontend", "backend", "fullstack"}),
        ("infrastructure", {"docker", "linux", "vps"}),
        ("sql", {"sql", "postgresql"}),
    ]:
        if keywords & words:
            return name
    return "python"


def generate_reply(project: Project) -> str:
    kind = reply_kind(project)
    topic = project.title[:140].rstrip()
    if kind == "clarify_stack":
        return (
            f"Здравствуйте! Увидел вашу задачу «{topic}».\n"
            "Готов посмотреть текущий проект и оценить объём, "
            "но сначала хотел бы уточнить используемый стек и характер доработок.\n\n"
            "Пришлите, пожалуйста, версии технологий, описание задачи и текущего проекта. "
            "После изучения смогу сказать, возьмусь ли за работу, и оценить срок и стоимость."
        )
    if kind == "partial_stack":
        return (
            f"Здравствуйте! Увидел вашу задачу «{topic}».\n"
            "Готов изучить задачу. Сначала хотел бы уточнить платформу, её версию, "
            "нужные изменения и ограничения текущего проекта.\n\n"
            "По этим данным оценю объём и смогу подтвердить возможность выполнения, срок и стоимость."
        )
    skills, required = TEMPLATES[kind]
    return (
        f"Здравствуйте! Увидел вашу задачу «{topic}».\n"
        f"Готов посмотреть проект. Работаю с {skills}.\n\n"
        f"Если пришлёте {required}, оценю объём, срок и точную стоимость.\n\n"
        "Могу приступить в ближайшее время."
    )


def recommended_price(project: Project, assessment: Assessment) -> str:
    low, high = ((5000, 10000) if assessment.complexity <= 3 else (10000, 20000) if assessment.complexity <= 5 else (20000, 30000))
    if project.currency == "RUB":
        if project.budget_min is not None:
            low = max(low, int(project.budget_min))
        if project.budget_max is not None:
            high = min(high, int(project.budget_max))
        if low > high or low > 30000 or high < 5000:
            return "Уточнить объём: бюджет не пересекается с ориентиром для этой сложности (5 000–30 000 RUB)."
    value = f"{low:,}–{high:,} RUB".replace(",", " ") if low != high else f"{low:,} RUB".replace(",", " ")
    note = " Для бюджета в другой валюте курс не применялся." if project.currency and project.currency != "RUB" else ""
    return value + " — предварительный ориентир, после уточнения ТЗ." + note
