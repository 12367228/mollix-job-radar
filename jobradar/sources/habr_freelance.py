"""Conservative Habr HTML parser; live /tasks is currently blocked by robots.txt.

Only the parser contract is covered by synthetic fixtures. No live HTML layout
could be verified on 2026-09-23, so restored access still requires re-verification.
"""
from datetime import datetime
import re
from urllib.parse import urlsplit

from .base import SourceAdapter, listing_soup, text_of
from ..http import SourceError
from ..models import Project
from ..normalization import parse_budget, parse_count, parse_publication_time, safe_url


class HabrFreelanceAdapter(SourceAdapter):
    name = "habr_freelance"
    url = "https://freelance.habr.com/tasks"
    allowed_hosts = frozenset({"freelance.habr.com"})

    def parse(self, html: str, *, now: datetime | None = None) -> list[Project]:
        soup = listing_soup(html)
        cards = soup.select("article.task")
        if not cards:
            raise SourceError("unrecognized_listing_layout")
        projects = []
        for card in cards[:100]:
            try:
                link = card.select_one(".task__title a[href]")
                if not link:
                    raise ValueError("missing_title_link")
                url = safe_url(str(link["href"]), base=self.url, allowed_hosts=self.allowed_hosts)
                match = re.fullmatch(r"/tasks/(\d+)/?", urlsplit(url).path)
                if not match:
                    raise ValueError("invalid_task_url")
                time = card.select_one("time[datetime]")
                lower, upper, currency = parse_budget(text_of(card.select_one(".task__price")))
                projects.append(Project(
                    source=self.name, source_id=match.group(1), title=text_of(link), url=url,
                    description=text_of(card.select_one(".task__description")) or None,
                    published_at=parse_publication_time(str(time["datetime"]), now) if time else None,
                    budget_min=lower, budget_max=upper, currency=currency,
                    responses_count=parse_count(text_of(card.select_one(".params__responses"))),
                    category=text_of(card.select_one(".task__tags")) or None,
                ))
            except (ValueError, TypeError, KeyError, ArithmeticError):
                self.parse_errors += 1
        if not projects:
            raise SourceError("all_cards_invalid")
        return projects
