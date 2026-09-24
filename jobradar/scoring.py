from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re

from .filtering import Relevance, relevance_gate
from .delivery import apply_delivery_gate
from .models import Project
from .safety import open_status_confirmed
from .stack import LOW_UNKNOWN_MATCH, PARTIAL_MATCH, assess_stack


@dataclass(frozen=True)
class ScoreReason:
    code: str
    points: int
    text: str


@dataclass(frozen=True)
class Assessment:
    score: int
    raw_score: int
    reasons: tuple[ScoreReason, ...]
    positive_keywords: tuple[str, ...]
    negative_keywords: tuple[str, ...]
    complexity: int
    eligible: bool
    relevance_gate: bool = True
    scored: bool = True
    risk_flags: tuple[str, ...] = ()
    age_hours: float | None = None
    hot: bool = False
    relevance_reason: str = ""
    stack_compatibility: str = LOW_UNKNOWN_MATCH
    stack_details: tuple[str, ...] = ()
    stack_reason: str = ""
    delivery_blockers: tuple[str, ...] = ("NOT_EVALUATED",)

    @property
    def telegram_deliverable(self) -> bool:
        return self.eligible and not self.delivery_blockers

    @property
    def label(self) -> str:
        if not self.eligible:
            return "SKIP"
        return "FIRE" if self.score >= 8 else "GOOD"

    def to_dict(self) -> dict:
        return {**asdict(self), "label": self.label, "telegram_deliverable": self.telegram_deliverable}


def project_age(project: Project, now: datetime) -> float | None:
    return (now - project.published_at).total_seconds() / 3600 if project.published_at else None


def experience_required(text: str) -> bool:
    # Optional/negated qualifications and the range 1–3 years must not mean 3+ required.
    text = re.sub(r"(?:опыт|кейсы|портфолио)\s+не\s+(?:требует\w*|обязател\w*|нуж\w*)", "", text)
    required = r"(?:требует\w*|требован\w*|обязател\w*|необходим\w*|опыт\b|experience\b|required|mandatory|must|только)"
    for years in re.finditer(r"(?<!\w)(\d+)\s*(?:[-–—]\s*(\d+)\s*)?(\+)?\s*(?:лет|год\w*|years?)", text):
        context = text[max(0, years.start() - 45):years.start()] + text[years.end():years.end() + 20]
        if int(years.group(1)) >= 3 and (years.group(3) or re.search(required, context)):
            return True
    return bool(re.search(
        required + r".{0,60}(?:кейс\w*|портфолио|senior)|"
        r"(?:кейс\w*|портфолио|senior).{0,40}(?:обязател\w*|required|mandatory|only)|"
        r"подтвержд[её]н\w*\s+(?:кейс\w*|внедрен\w*)|рекомендаци\w*\s+клиент\w*|verified\s+(?:cases|portfolio)", text))


def assess_project(project: Project, *, now: datetime | None = None,
                   max_project_age_hours: int = 24, hot_project_age_hours: int = 6) -> Assessment:
    assessment = _assess_project(project, now=now, max_project_age_hours=max_project_age_hours,
                                 hot_project_age_hours=hot_project_age_hours)
    return apply_delivery_gate(project, assessment)


def _assess_project(project: Project, *, now: datetime | None = None,
                    max_project_age_hours: int = 24, hot_project_age_hours: int = 6) -> Assessment:
    now = now or datetime.now(timezone.utc)
    stack = assess_stack(project)
    stack_fields = dict(stack_compatibility=stack.level, stack_details=stack.matches, stack_reason=stack.reason)
    if project.discovery_skip_reason:
        reason = {"advertisement": "Рекламный пост", "education": "Продвижение обучения",
                  "full_time_vacancy": "Full-time вакансия без признаков разового проекта"}[project.discovery_skip_reason]
        return Assessment(0, 0, (ScoreReason(project.discovery_skip_reason, 0, reason),), (), (), 6 if stack.unsupported else 0, False,
                          relevance_gate=False, scored=False, age_hours=project_age(project, now), relevance_reason=reason,
                          risk_flags=("STACK_MISMATCH",) if stack.mismatch else (), **stack_fields)
    relevance = relevance_gate(project)
    if not relevance.passed:
        # score=0 is a storage sentinel; CLI shows "not scored", JSON includes scored=false.
        return Assessment(0, 0, (ScoreReason(relevance.code, 0, relevance.reason),),
                          relevance.matches.positive, relevance.matches.negative, 6 if stack.unsupported else 0, False,
                          relevance_gate=False, scored=False, age_hours=project_age(project, now),
                          relevance_reason=relevance.reason, risk_flags=("STACK_MISMATCH",) if stack.mismatch else (), **stack_fields)
    return score_project(project, now=now, max_project_age_hours=max_project_age_hours,
                         hot_project_age_hours=hot_project_age_hours, _relevance=relevance)


def score_project(project: Project, *, now: datetime | None = None,
                  max_project_age_hours: int = 24, hot_project_age_hours: int = 6,
                  _relevance: Relevance | None = None) -> Assessment:
    # Keep existing callers safe; only the gated branch below performs scoring.
    if _relevance is None:
        return assess_project(project, now=now, max_project_age_hours=max_project_age_hours,
                              hot_project_age_hours=hot_project_age_hours)
    now = now or datetime.now(timezone.utc)
    matches = _relevance.matches
    max_project_age_hours = min(24, max_project_age_hours)
    hot_project_age_hours = min(6, hot_project_age_hours, max_project_age_hours)
    text = f"{project.title} {project.description or ''}".lower()
    reasons: list[ScoreReason] = []
    risks: list[str] = []
    complexity, allowed = 3, True

    def add(code: str, points: int, explanation: str) -> None:
        reasons.append(ScoreReason(code, points, explanation))

    add("base", 2, "Базовая оценка технического проекта")
    groups = [
        {"telegram bot", "telegram-бот", "бот", "chatbot", "aiogram"},
        {"python", "django", "fastapi", "flask"}, {"api", "rest api", "webhook", "интеграция"},
        {"n8n", "автоматизация", "automation"}, {"парсер", "парсинг", "scraping"},
        {"react", "javascript", "typescript", "node.js", "доработать сайт", "доработка сайта", "bug", "ошибка", "backend", "frontend", "fullstack"},
        {"1с", "унф"}, {"docker", "linux", "vps"}, {"sql", "postgresql"},
    ]
    count = sum(bool(set(matches.positive) & group) for group in groups)
    add("specialization", min(4, count + 1), _relevance.reason)
    stack = assess_stack(project)
    add("stack_compatibility", stack.points, f"STACK_COMPATIBILITY: {stack.level}; {stack.reason}")
    if stack.mismatch:
        risks.append("STACK_MISMATCH")
    if stack.deep_1c:
        risks.append("DEEP_1C")
        add("deep_1c", 0, "DEEP_1C: глубокая разработка или внедрение 1С; опыт не подтверждён")
    age = project_age(project, now)
    if not project.publication_confirmed or not project.publication_evidence:
        add("unconfirmed_publication", 0, "Hard skip: время публикации не подтверждено исходной биржей")
        risks.append("UNCONFIRMED_PUBLICATION")
        allowed = False
    if not project.direct_url:
        add("invalid_direct_url", 0, "Hard skip: нет прямой ссылки на карточку исходной биржи")
        risks.append("INVALID_DIRECT_URL")
        allowed = False
    if project.open_status in {"closed", "awarded", "not_accepting"}:
        add("closed_project", 0, "Hard skip: проект закрыт, исполнитель выбран или отклики не принимаются")
        risks.append("PROJECT_NOT_OPEN")
        allowed = False
    if age is None:
        add("unknown_date", 0, "Hard skip: нет достоверного времени публикации")
        allowed = False
    elif age < 0:
        add("future_date", 0, "Hard skip: дата публикации в будущем; свежесть не подтверждена")
        allowed = False
    elif age > max_project_age_hours:
        add("stale", 0, f"Hard skip: старше {max_project_age_hours} часов")
        allowed = False
    else:
        points = 3 if age <= 2 else 2 if age <= 6 else 1 if age <= 12 else 0
        add("fresh", points, f"Возраст {age:.1f} ч; бонус свежести {points:+d}")
    description = project.description or ""
    if len(description) >= 80 and re.search(r"добав\w*|исправ\w*|настро\w*|интегр\w*|получ\w*|выгруз\w*|сохран\w*|endpoint|webhook|csv|json|xlsx", description, re.I):
        add("concrete", 1, "Есть конкретные действия или формат результата в ТЗ")
    # Use the offered ceiling, or the sole known bound; never invent a currency conversion.
    budget = project.budget_max if project.budget_max is not None else project.budget_min
    if budget is None:
        add("unknown_budget", 0, "Бюджет не указан")
    elif project.currency != "RUB":
        add("foreign_budget", 0, "Валюта не RUB или неизвестна: оценка по курсу не выполнялась")
    elif budget < 3000:
        add("budget_too_low", 0, "Hard skip: бюджет ниже 3 000 RUB")
        allowed = False
    elif budget < 5000:
        add("budget", -2, "Бюджет 3 000–4 999 RUB")
    elif budget <= 30000:
        add("budget", 2, "Бюджет в целевом диапазоне 5 000–30 000 RUB")
    elif budget <= 100000:
        add("budget", 1, "Бюджет выше целевого диапазона, до 100 000 RUB")
    else:
        add("budget", 0, "Бюджет больше 100 000 RUB")
        risks.append("LARGE_PROJECT")
    responses = project.responses_count
    if responses is None:
        add("unknown_responses", 0, "Количество откликов неизвестно")
        risks.append("RESPONSES_UNKNOWN")
    else:
        points = 3 if responses <= 3 else 2 if responses <= 7 else 1 if responses <= 15 else -1 if responses <= 20 else -3 if responses <= 30 else -5
        add("competition", points, f"Откликов: {responses}; конкуренция {points:+d}")
        if responses > 20:
            risks.append("HIGH_COMPETITION")
    if re.search(r"доработ\w*|исправ\w*|bug\s*fix|существующ\w*|готов\w*\s+(?:сайт|проект|бот|код)", text):
        add("existing", 1, "Доработка или исправление существующего проекта")
    duration = re.search(r"(?:срок(?:\s+выполнения)?\s*[:—-]?|за|до)\s*(\d+)\s*(?:[-–—]\s*(\d+)\s*)?\+?\s*(?:рабочих\s+)?(?:дн\w*|день|days?)", text)
    days = int(duration.group(2) or duration.group(1)) if duration else None
    if days is not None:
        if 1 <= days <= 5:
            add("short_task", 1, "Указан срок 1–5 дней")
        elif 10 < days < 30:
            add("long_task", -2, "Указанный срок больше 10 дней")
            complexity += 2
    product = r"(?:маркетплейс\w*|marketplace|мобильн\w*\s+приложени\w*|mobile\s+app|saas(?:\s+platform)?|crm|erp)"
    scratch = r"(?:с\s+нуля|from\s+scratch)"
    large = bool(re.search(product + r".{0,70}" + scratch + "|" + scratch + r".{0,70}" + product +
                           r"|enterprise\s+implementation|(?:корпоративн\w*|масштабн\w*)\s+внедрен\w*|внедрен\w*.{0,25}\berp\b", text))
    if large or (days is not None and days >= 30):
        if "LARGE_PROJECT" not in risks:
            risks.append("LARGE_PROJECT")
    if experience_required(text):
        risks.append("EXPERIENCE_GATE")
    if "LARGE_PROJECT" in risks:
        add("large_project", -3, "LARGE_PROJECT: крупный бюджет, продукт с нуля, внедрение ERP/enterprise или срок 30+ дней")
        complexity += 3
    if "EXPERIENCE_GATE" in risks:
        add("experience_gate", -3, "EXPERIENCE_GATE: требуется опыт 3+ года, обязательные кейсы, внедрения, рекомендации или Senior")
        complexity += 2
    if any(k in matches.positive for k in ("1с", "унф", "api", "docker")):
        complexity += 1
    if stack.mismatch or stack.deep_1c:
        complexity = max(6, complexity)
    if project.open_status in {"unknown", "open"} and not open_status_confirmed(project):
        risks.append("OPEN_STATUS_UNKNOWN")
    raw_score = sum(reason.points for reason in reasons)
    result = max(0, min(10, raw_score))
    if not allowed:
        result = min(5, result)
        add("hard_skip", 0, "Отправка запрещена safety gate или бюджетом; итог не выше 5")
    if allowed and age > hot_project_age_hours:
        result = min(7, result)
        risks.append("OLDER_THAN_HOT_WINDOW")
        add("fire_age_limit", 0, f"FIRE запрещён: старше {hot_project_age_hours} часов; итог не выше 7")
    if "LARGE_PROJECT" in risks or "EXPERIENCE_GATE" in risks:
        result = min(7, result)
        add("fire_blocked", 0, "FIRE запрещён: крупный проект или обязательный опыт; итог не выше 7")
    if responses is None or responses > 20:
        cap = 6 if responses is not None and responses > 30 else 7
        result = min(cap, result)
        add("competition_cap", 0, f"FIRE запрещён: число откликов неизвестно или больше 20; итог не выше {cap}")
    if stack.level == PARTIAL_MATCH:
        result = min(7, result)
        add("partial_stack_cap", 0, "PARTIAL_MATCH: FIRE запрещён, максимум GOOD 7/10")
    if "OPEN_STATUS_UNKNOWN" in risks:
        result = min(7, result)
        add("open_status_cap", 0, "Нет подтверждения приёма откликов: максимум GOOD 7/10")
    if stack.deep_1c:
        result = min(6, result)
        add("deep_1c_cap", 0, "DEEP_1C: максимум GOOD 6/10")
    if stack.mismatch:
        result = min(6, result)
        add("stack_score_cap", 0, "STACK_MISMATCH: FIRE запрещён, максимум GOOD 6/10")
    return Assessment(result, raw_score, tuple(reasons), matches.positive, matches.negative,
                      max(1, min(10, complexity)), allowed and result >= 6,
                      risk_flags=tuple(risks), age_hours=age,
                      hot=age is not None and 0 <= age <= min(hot_project_age_hours, max_project_age_hours),
                      relevance_reason=_relevance.reason, stack_compatibility=stack.level,
                      stack_details=stack.matches, stack_reason=stack.reason)
