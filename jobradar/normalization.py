from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import html
import re
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

MSK = timezone(timedelta(hours=3))


def clean_text(value: str | None, limit: int = 20000) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", html.unescape(str(value)))
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C") or c in "\n\t")
    return re.sub(r"\s+", " ", value).strip()[:limit]


def html_text(value: str | None) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    return clean_text(soup.get_text(" ", strip=True))


def safe_url(value: str, *, base: str = "", allowed_hosts: set[str] | frozenset[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("invalid_url")
    if re.search(r"[\s\\\x00-\x1f\x7f]", value) or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I):
        raise ValueError("invalid_url")
    parts = urlsplit(urljoin(base, value))
    try:
        port = parts.port
    except ValueError:
        raise ValueError("invalid_url") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password or port not in (None, 443):
        raise ValueError("invalid_url")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ValueError("untrusted_url_host")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def parse_count(value: str | None) -> int | None:
    text = clean_text(value)
    match = re.fullmatch(r"([\d\s]+)(?:\s+[^\d]+)?", text)
    if not match:
        return None
    try:
        return int(re.sub(r"\s", "", match.group(1)))
    except ValueError:
        return None


def parse_budget(value: str | None) -> tuple[Decimal | None, Decimal | None, str | None]:
    text = clean_text(value).lower()
    if not text or re.search(r"договор|обсужда|не указан|по соглашению", text):
        return None, None, None
    currency = next((code for pattern, code in [
        (r"₽|\bруб\w*|\brub\b|\brur\b", "RUB"),
        (r"\$|\busd\b", "USD"), (r"€|\beur\b", "EUR"),
        (r"₴|\bгрн\b|\buah\b", "UAH"),
    ] if re.search(pattern, text)), None)
    # Hourly/monthly compensation is not a total project budget.
    if not currency or re.search(r"/\s*(?:ч(?:ас)?\b|h\b|hour|мес)|в\s+(?:час|месяц)|per\s+(?:hour|month)", text):
        return None, None, None
    numbers = re.findall(r"\d+(?:[ .]\d{3})*(?:[,.]\d{1,2})?", text)
    try:
        amounts = [Decimal(re.sub(r"[ .](?=\d{3}(?:\D|$))", "", n).replace(" ", "").replace(",", ".")) for n in numbers[:2]]
    except InvalidOperation:
        return None, None, currency
    if not amounts:
        return None, None, currency
    if re.search(r"\b(?:тыс|k)\b", text):
        amounts = [amount * 1000 for amount in amounts]
    if len(amounts) == 2 and re.search(r"[–—-]|\bдо\b", text):
        return min(amounts), max(amounts), currency
    if re.search(r"\b(?:до|up to)\b", text):
        return None, amounts[0], currency
    if re.search(r"\b(?:от|from)\b", text):
        return amounts[0], None, currency
    return amounts[0], amounts[0], currency


def parse_date(value: str | None, now: datetime | None = None) -> datetime | None:
    text = clean_text(value).lower()
    if not text:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    try:
        result = datetime.fromisoformat(text.replace("z", "+00:00"))
        return (result if result.tzinfo else result.replace(tzinfo=MSK)).astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=MSK).astimezone(timezone.utc)
        except ValueError:
            pass
    if text in ("только что", "сейчас"):
        return now.astimezone(timezone.utc)
    relative = re.fullmatch(r"(?:(\d+)\s+)?(секунд\w*|минут\w*|час\w*|день|дня|дней) назад", text)
    if relative:
        number = int(relative.group(1) or 1)
        unit = relative.group(2)
        seconds = 1 if unit.startswith("секунд") else 60 if unit.startswith("минут") else 3600 if unit.startswith("час") else 86400
        return (now - timedelta(seconds=number * seconds)).astimezone(timezone.utc)
    today = re.fullmatch(r"(сегодня|вчера)(?:\s+(?:в\s+)?(\d{1,2}:\d{2}))?", text)
    if today:
        result = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if today.group(1) == "вчера":
            result -= timedelta(days=1)
        if today.group(2):
            try:
                hour, minute = map(int, today.group(2).split(":"))
                result = result.replace(hour=hour, minute=minute)
            except ValueError:
                return None
        return result.astimezone(timezone.utc)
    return None


def parse_publication_time(value: str | None, now: datetime | None = None) -> datetime | None:
    """A calendar day alone is insufficient for the 24-hour delivery gate."""
    text = clean_text(value).lower()
    now = now or datetime.now(timezone.utc)
    if not text:
        return None
    relative = re.fullmatch(r"((?:\d+\s+(?:час\w*|минут\w*|секунд\w*)\s*)+)назад", text)
    if relative:
        seconds = sum(int(number) * (3600 if unit.startswith("час") else 60 if unit.startswith("минут") else 1)
                      for number, unit in re.findall(r"(\d+)\s+(час\w*|минут\w*|секунд\w*)", relative.group(1)))
        return (now - timedelta(seconds=seconds)).astimezone(timezone.utc)
    if re.search(r"\b(?:день|дня|дней) назад$", text):
        # Day-rounded relative ages are not reliable to the hour.
        return None
    if text not in {"сейчас", "только что"} and not re.search(r"\d{1,2}:\d{2}", text):
        return None
    return parse_date(text, now)
