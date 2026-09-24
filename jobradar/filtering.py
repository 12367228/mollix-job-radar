"""Relevance is decided before scoring; incidental technology names are insufficient."""

from dataclasses import dataclass
import re

from .models import Project
from .normalization import clean_text

POSITIVE_KEYWORDS = (
    "telegram bot", "telegram-бот", "бот", "chatbot", "python", "django", "fastapi",
    "flask", "api", "rest api", "webhook", "n8n", "automation", "автоматизация",
    "парсер", "парсинг", "scraping", "react", "javascript", "typescript", "node.js",
    "доработать сайт", "доработка сайта", "bug", "ошибка", "интеграция", "1с", "унф",
    "docker", "linux", "vps", "aiogram", "backend", "frontend", "fullstack", "sql", "postgresql",
)
NEGATIVE_KEYWORDS = (
    "копирайтинг", "отзывы", "карточки товаров", "анкеты", "powerpoint", "презентации",
    "photoshop", "ретушь", "перевод", "контент-менеджмент", "наполнение сайта",
    "продажи", "поиск клиентов", "холодные звонки", "дизайн", "логотип", "excel",
    "google docs", "оператор маркетплейса", "рассылка контента", "тексты", "smm",
    "seo продвижение", "монтаж", "анимация", "3d",
)
ALIASES = {
    "telegram bot": r"telegram[\s-]+bots?",
    "telegram-бот": r"telegram[\s-]+бот(?:а|ы|ов|у|ом|ами)?",
    "бот": r"бот(?:а|ы|ов|у|ом|ами)?|bots?",
    "автоматизация": r"автоматизац\w*|автоматизир\w*",
    "парсер": r"парсер\w*", "парсинг": r"парсинг\w*",
    "доработать сайт": r"доработ\w*\s+сайт\w*",
    "доработка сайта": r"доработ\w*\s+сайт\w*",
    "bug": r"bugs?|баг\w*", "ошибка": r"ошибк\w*", "интеграция": r"интеграц\w*",
    "1с": r"1[сc](?::\w+)?", "дизайн": r"дизайн\w*", "логотип": r"логотип\w*",
    "копирайтинг": r"копирайт\w*", "тексты": r"текст(?:ы|ов|ами)",
    "отзывы": r"отзыв\w*", "карточки товаров": r"карточ\w*\s+(?:товар\w*|маркетплейс\w*)",
    "анкеты": r"анкет\w*", "презентации": r"презентац\w*|слайд\w*",
    "photoshop": r"photoshop|фотошоп\w*", "ретушь": r"ретуш\w*",
    "перевод": r"перевод\w*|перевести|translation",
    "контент-менеджмент": r"контент[-\s]+менедж\w*",
    "наполнение сайта": r"(?:наполн\w*|заполн\w*)\s+сайт\w*",
    "продажи": r"продаж\w*", "поиск клиентов": r"(?:поиск|искать|найти)\s+клиент\w*",
    "холодные звонки": r"холодн\w*\s+звон\w*",
    "оператор маркетплейса": r"(?:оператор|менеджер)\w*\s+маркетплейс\w*",
    "рассылка контента": r"(?:рассыл\w*|размещ\w*|публиков\w*)\s+контент\w*",
    "seo продвижение": r"seo[-\s]+продвижени\w*",
}


def contains(text: str, keyword: str) -> bool:
    pattern = ALIASES.get(keyword, re.escape(keyword))
    return re.search(r"(?<!\w)(?:" + pattern + r")(?!\w)", text, re.I) is not None


@dataclass(frozen=True)
class KeywordMatches:
    positive: tuple[str, ...]
    negative: tuple[str, ...]
    programming: bool


@dataclass(frozen=True)
class Relevance:
    passed: bool
    code: str
    reason: str
    matches: KeywordMatches


def _technical_work(text: str) -> bool:
    # Require implementation intent, not just articles about Python or bot reviews.
    return bool(re.search(
        r"\bавтоматизир\w*|\b(?:automation|автоматизация|парсинг|scraping)\s+(?:процесс\w*|заполн\w*|карточ\w*|данн\w*|сбор\w*)|"
        r"\b(?:разработ\w*|созда\w*|напис\w*|доработ\w*|исправ\w*|настро\w*|внедр\w*|интегр\w*|подключ\w*|build|develop|fix)\s+"
        r"(?:и\s+(?:адаптивн\w*\s+)?в[её]рстк\w*\s+)?"
        r"(?:(?:нов\w*|существующ\w*|небольш\w*|программ\w*|telegram|aiogram|python|react|rest|веб|web)[\s-]+){0,3}"
        r"(?:бот\w*|bots?\b|chatbot\b|api\b|webhook\b|n8n\b|парсер\w*|скрипт\w*|код\w*|"
        r"приложени\w*|app\b|сайт\w*|баг\w*|интеграц\w*|модул\w*|сервис\w*|1[сc]\b|"
        r"saas\b|crm\b|erp\b|маркетплейс\w*|marketplace\b|плагин\w*|комплекс\w*|обеспечени\w*)|"
        r"\b(?:бот\w*|скрипт\w*|парсер\w*|api|n8n)\s+(?:для|на|через)\b", text, re.I))


def relevance_gate(project: Project) -> Relevance:
    title = clean_text(project.title).lower()
    body = clean_text(f"{title} {project.description or ''}").lower()
    category = (project.category or "").lower()
    positive = tuple(word for word in POSITIVE_KEYWORDS if contains(body, word))
    negative = tuple(word for word in NEGATIVE_KEYWORDS if contains(body, word))
    technical_category = bool(re.search(
        r"программирован\w*|\bprogramming\b|программное\s+обеспечение|(?:веб|web)[-\s]+(?:разработ\w*|development)|"
        r"chatbot\s+development|разработ\w*\s+(?:чат[-\s]*)?бот\w*|automation|автоматизац\w*|"
        r"\b(?:api|python|1[сc]|devops)\b", category, re.I))
    # Explicit software construction covers SaaS from scratch even without a stack/category.
    software_build = _technical_work(title) and bool(re.search(
        r"\b(?:saas|crm|erp|marketplace|маркетплейс\w*|mobile\s+app|мобильн\w*\s+приложени\w*)\b", title))
    title_negative = any(contains(title, word) for word in NEGATIVE_KEYWORDS)
    title_manual_action = bool(re.search(
        r"вручную|копирайт\w*|контент[-\s]+менедж\w*|оператор\w*|холодн\w*\s+звон\w*|"
        r"(?:напис\w*|писать|созда\w*|запол\w*|напол\w*|состав\w*|исправ\w*|размещ\w*|рассыл\w*)"
        r".{0,45}(?:отзыв\w*|стать\w*|слайд\w*|презентац\w*|анкет\w*|карточ\w*|прайс\w*|текст\w*|контент\w*)", title))
    manual_primary = (title_negative and title_manual_action and not _technical_work(title)) or (
        bool(negative) and not _technical_work(body))
    if _technical_work(title):
        manual_primary = False
    programming = bool(positive) or technical_category or software_build
    matches = KeywordMatches(positive, negative, programming and not manual_primary)
    if manual_primary:
        return Relevance(False, "manual_or_nontechnical", "Основная задача — ручная или непрофильная работа: " + ", ".join(negative[:5]), matches)
    if not programming:
        return Relevance(False, "no_technical_signal", "Нет технических ключевых слов или категории; Telegram сам по себе недостаточен", matches)
    reason = "Технические признаки: " + ", ".join(positive[:8]) if positive else (
        "Техническая категория: " + category if technical_category else "Явная разработка программного продукта")
    return Relevance(True, "technical_relevance", reason, matches)


def match_keywords(project: Project) -> KeywordMatches:
    return relevance_gate(project).matches
