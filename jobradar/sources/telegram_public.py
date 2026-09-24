"""Unauthenticated t.me/s web previews, completely separate from the Telegram Bot API."""
from datetime import datetime
import re
from urllib.parse import urlsplit

from .base import SourceAdapter, listing_soup, text_of
from ..deduplication import project_link
from ..http import SourceError
from ..models import Project
from ..normalization import clean_text, parse_budget, parse_publication_time, safe_url


def post_skip_reason(text: str) -> str | None:
    text = text.lower()
    if re.search(r"\berid\b|#реклама\b|на правах рекламы|рекламный пост|промокод|купить рекламу|разместить рекламу", text):
        return "advertisement"
    if re.search(r"(?:записывай\w*|регистрируй\w*|приглашаем|регистрация на).{0,70}(?:курс|вебинар|интенсив)|"
                 r"(?:бесплатный|обучающий)\s+(?:вебинар|курс)|(?:курс|вебинар|интенсив).{0,50}(?:скидк\w*|запись открыта)", text):
        return "education"
    full_time = re.search(r"full[-\s]?time|полная занятость|в штат|на постоянную работу|5/2|40 часов в неделю|"
                          r"трудоустройств\w*|зарплата|оклад|/\s*мес\b|в месяц|#вакансия\b", text)
    project_work = re.search(r"разов\w*\s+(?:задач\w*|проект\w*|работ\w*)|проектн\w*\s+(?:занятость|работ\w*)|"
                             r"сдельн\w*|фиксированн\w*\s+бюджет", text)
    return "full_time_vacancy" if full_time and not project_work else None


class TelegramPublicAdapter(SourceAdapter):
    name = "telegram_public"
    url = "https://t.me/"
    allowed_hosts = frozenset({"t.me"})

    def __init__(self, channels: tuple[str, ...]):
        super().__init__()
        if not channels or len(channels) > 20 or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", c) for c in channels):
            raise ValueError("invalid_public_channels")
        self.channels = tuple(dict.fromkeys(c.lower() for c in channels))

    @property
    def discovery_urls(self) -> list[str]:
        return [f"https://t.me/s/{channel}" for channel in self.channels]

    def fetch(self, client, *, now):
        return self.fetch_pages(client, self.discovery_urls, now=now) if self.enabled else []

    def parse(self, html: str, *, now: datetime | None = None) -> list[Project]:
        filtered_before = self.filtered_count
        soup = listing_soup(html)
        posts = soup.select(".tgme_widget_message[data-post]")
        if not posts:
            raise SourceError("public_preview_unavailable_or_empty")
        projects = []
        for post in posts[:100]:
            try:
                match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{4,31})/(\d+)", str(post.get("data-post", "")))
                if not match or match.group(1).lower() not in self.channels:
                    raise ValueError("unexpected_post_channel")
                channel, number = match.group(1).lower(), match.group(2)
                node = post.select_one(".tgme_widget_message_text")
                if node is None or not text_of(node):
                    self.filtered_count += 1  # photo-only/service posts have no assessable brief
                    continue
                lines = [clean_text(line) for line in node.get_text("\n", strip=True).splitlines() if clean_text(line)]
                title = next((line for line in lines if not re.fullmatch(r"(?:#\w+\s*)+", line)), lines[0])[:180]
                description = text_of(node)
                url = f"https://t.me/{channel}/{number}"
                time = post.select_one(".tgme_widget_message_date time[datetime]")
                timestamp = str(time["datetime"]) if time else ""
                # Telegram supplies ISO timestamps with timezone; never infer one from clock text.
                published = parse_publication_time(timestamp, now) if re.search(r"(?:Z|[+-]\d{2}:\d{2})$", timestamp) else None
                links = []
                for a in node.select("a[href]"):
                    try:
                        target = safe_url(str(a["href"]))
                    except ValueError:
                        continue
                    parts = urlsplit(target)
                    if target == url or parts.path in {"", "/"}:
                        continue
                    if parts.hostname == "t.me" and not project_link(target):
                        continue  # usernames, invite links and bot links are not project URLs
                    links.append(target)
                external = next((link for link in links if project_link(link)), links[0] if links else None)
                budget_line = next((line for line in lines if re.match(r"^(?:💰\s*)?(?:бюджет|оплата|стоимость)\s*:", line, re.I)), "")
                lower, upper, currency = parse_budget(budget_line)
                forwarded = post.select_one(".tgme_widget_message_forwarded_from") is not None
                projects.append(Project(
                    source=self.name, source_id=f"{channel}_{number}", channel=channel, title=title,
                    description=description, url=url, published_at=published,
                    budget_min=lower, budget_max=upper, currency=currency, external_url=external,
                    origin_kind="repost" if forwarded or external else "direct_post",
                    discovery_skip_reason=post_skip_reason(description),
                ))
            except (ValueError, TypeError, KeyError, ArithmeticError):
                self.parse_errors += 1
        if not projects and self.filtered_count == filtered_before:
            raise SourceError("all_cards_invalid")
        return projects
