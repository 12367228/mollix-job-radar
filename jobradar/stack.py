"""Conservative stack fit, shared by scoring and reply drafts.

Specific requirements override generic API/automation/JS words. Category alone
cannot establish stack experience; missing evidence is LOW_UNKNOWN_MATCH.
"""
from dataclasses import dataclass
import re

from .models import Project

HIGH_MATCH = "HIGH_MATCH"
PARTIAL_MATCH = "PARTIAL_MATCH"
LOW_UNKNOWN_MATCH = "LOW_UNKNOWN_MATCH"

HIGH = {
    "Python": r"python|питон\w*|django|flask",
    "Telegram Bot API": r"telegram[\s-]+(?:bot(?:s|\s+api)?|бот\w*)|бот\w*\s+(?:для|в)\s+telegram",
    "aiogram": r"aiogram", "FastAPI": r"fastapi",
    "REST API": r"(?:rest\s+)?api", "webhooks": r"webhooks?|вебху\w*",
    "n8n": r"n8n", "automation": r"automation|автоматизац\w*|автоматизир\w*",
    "parsing/scraping": r"parsing|scraping|парсинг\w*|парсер\w*|скрейпинг\w*",
    "JavaScript": r"javascript|js", "TypeScript": r"typescript|ts",
    "React": r"react(?:\.js|js)?", "Node.js": r"node(?:\.js|js)",
    "Docker": r"docker", "Linux": r"linux", "VPS": r"vps|vds",
    "SQL/PostgreSQL": r"sql|postgresql|postgres",
}
PARTIAL = {
    "PHP": r"php(?:\s*\d+(?:\.\d+)*)?|laravel|symfony",
    "WordPress": r"wordpress|вордпресс", "OpenCart": r"opencart|опенкарт",
    "payment integrations": r"payment\s+integrations?|плат[её]жн\w*\s+(?:систем\w*|шлюз\w*|интеграц\w*)|"
                            r"(?:интеграц\w*|подключ\w*)\s+(?:оплат\w*|плат[её]ж\w*)|stripe|paddle|lemonsqueezy|юкасса|yookassa",
    "Google APIs": r"google\s+(?:(?:maps|sheets|drive|calendar|ads|analytics|docs)\s+)?apis?|"
                   r"api\s+google(?:\s+\w+)?",
    "Bitrix": r"bitrix(?:24)?|битрикс(?:24)?",
}
LOW = {
    "Ruby / Ruby on Rails": r"ruby(?:\s+on\s+rails)?|rails|ror",
    "Java": r"java(?:\s*\d+)?|джава|spring(?:\s+boot)?",
    "C# / .NET": r"c#|csharp|c-sharp|\.net|asp\.net",
    "C++": r"c\+\+(?:\d+)?", "Flutter": r"flutter|dart",
    "Kotlin": r"kotlin", "Swift": r"swift",
    "Android": r"android|андроид\w*", "Tilda": r"tilda|тильд\w*",
    "Cleverence": r"cleverence|клеверенс\w*",
    "GetCourse": r"get\s*course|гет\s*курс\w*",
    # Explicit requirements outside the confirmed profile are unknown too.
    "Go": r"golang|go", "Rust": r"rust", "Elixir": r"elixir",
    "Windows/RDP": r"windows(?:\s+server)?|rdp",
}


def mentions(text: str, pattern: str) -> bool:
    for match in re.finditer(r"(?<!\w)(?:" + pattern + r")(?!\w)", text, re.I):
        before, after = text[max(0, match.start() - 40):match.start()], text[match.end():match.end() + 50]
        # An explicit exclusion is not a requirement. 'No experience required'
        # does not exclude the language itself, so keep that conservative.
        if re.search(r"(?:без|не используем|not using|without)\s*$", before, re.I):
            continue
        if not re.search(r"опыт\w*\s*$", before, re.I) and re.match(
                r"\s+(?:не\s+(?:нуж\w*|требует\w*|использует\w*)|not\s+(?:required|needed))\b", after, re.I):
            continue
        return True
    return False


@dataclass(frozen=True)
class StackCompatibility:
    level: str
    points: int
    matches: tuple[str, ...]
    unsupported: tuple[str, ...]
    reason: str
    deep_1c: bool = False

    @property
    def mismatch(self) -> bool:
        return self.level == LOW_UNKNOWN_MATCH


def assess_stack(project: Project) -> StackCompatibility:
    text = f"{project.title}\n{project.description or ''}"
    high = [name for name, pattern in HIGH.items() if mentions(text, pattern)]
    partial = [name for name, pattern in PARTIAL.items() if mentions(text, pattern)]
    low = [name for name, pattern in LOW.items() if mentions(text, pattern)]
    deep_1c = False
    if mentions(text, r"1[сc](?::\w+)?|унф"):
        integration = bool(re.search(r"интеграц\w*|обмен\w*|\bapi\b|integration|exchange|прост\w*\s+расширени\w*|simple\s+extension", text, re.I))
        partial.append("1C integration / exchange / simple extension" if integration else "1C programming")
        deep_1c = bool(re.search(
            r"(?:перенос\w*|перенест\w*|миграц\w*).{0,60}(?:полноценн\w*|полн\w*|вс[еюя]\w*).{0,25}конфигурац\w*|"
            r"(?:перенос\w*|перенест\w*|миграц\w*)\s+(?:баз\w*(?:\s+данн\w*)?\s+и\s+)?конфигурац\w*|"
            r"(?:архитектур\w*|переработ\w*).{0,40}конфигурац\w*|"
            r"(?:сложн\w*|крупн\w*).{0,35}(?:erp|ер[пp]|модул\w*)|"
            r"(?:крупн\w*|масштабн\w*|комплексн\w*|полноценн\w*).{0,30}внедрен\w*|"
            r"(?:разработ\w*|внедрен\w*|внедрить).{0,40}\berp\b|"
            r"(?:full\s+)?configuration\s+(?:migration|transfer)|configuration\s+architecture|"
            r"complex\s+(?:erp\s+)?modules?|large[- ]scale\s+implementation", text, re.I | re.S))

    # Catch explicitly named, otherwise unlisted requirements (e.g. 'Стек: React,
    # Phoenix' or 'backend на Haskell'), without treating every English word as a stack.
    declared = re.findall(r"(?:стек|stack|язык|framework|фреймворк|технологии)\s*[:—-]\s*([^;\n]{1,160})", text, re.I)
    tokens = []
    for declaration in declared:
        declaration = re.split(r"[.!?](?=\s+[А-ЯA-Z])", declaration, maxsplit=1)[0]
        tokens.extend(re.findall(r"(?<!\w)(?:c\+\+\d*|c#|\.net|[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*)", declaration, re.I))
    tokens.extend(re.findall(
        r"(?:\bна|\busing|\brequires|\bтребуется|\bобязателен|\bнужен|\bиспользуется)\s+"
        r"(?:(?:язык|фреймворк|стек|разработчик|программист)\s+)?([A-Za-z][A-Za-z0-9.+#-]*)", text, re.I))
    for token in tokens:
        token = token.rstrip(".")
        if token.casefold() in {"and", "or", "with", "on", "the", "a", "an", "и", "bot", "rest", "google", "telegram",
                               "not", "required", "needed", "senior", "middle", "junior", "developer", "backend", "frontend", "fullstack"}:
            continue
        if not any(mentions(token, pattern) for pattern in [*HIGH.values(), *PARTIAL.values(), *LOW.values()]):
            if token.casefold() not in {name.casefold() for name in low}:
                low.append(token)

    matches = tuple([*low, *partial, *high])
    if low:
        return StackCompatibility(LOW_UNKNOWN_MATCH, -3, matches, tuple(low),
                                  "Не подтверждён опыт с обязательным стеком: " + ", ".join(low), deep_1c=deep_1c)
    if partial:
        return StackCompatibility(PARTIAL_MATCH, 0, matches, (), "Частичное совпадение: " + ", ".join(partial), deep_1c=deep_1c)
    if high:
        return StackCompatibility(HIGH_MATCH, 2, matches, (), "Основной стек: " + ", ".join(high))
    return StackCompatibility(LOW_UNKNOWN_MATCH, -3, (), (), "Стек не указан или не распознан; опыт не подтверждён")
