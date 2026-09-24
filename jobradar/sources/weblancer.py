from datetime import datetime
import re
from urllib.parse import urlsplit

from .base import SourceAdapter, listing_soup, text_of
from ..http import SourceError
from ..logging_utils import event
from ..models import Project
from ..normalization import parse_budget, parse_count, parse_publication_time, safe_url

WEB_CATEGORIES = {
    "veb-programmirovanie": "Веб-программирование",
    "prikladnoe-po": "Прикладное ПО",
    "sistemnoe-administrirovanie": "Системное администрирование",
    "dizain-saitov": "Дизайн сайтов",
    "illyustratsii-i-risunki": "Иллюстрации и рисунки",
    "kopiraiting": "Копирайтинг",
}


class WeblancerAdapter(SourceAdapter):
    name = "weblancer"
    url = "https://www.weblancer.net/freelance/"
    allowed_hosts = frozenset({"www.weblancer.net", "weblancer.net"})

    def parse(self, html: str, *, now: datetime | None = None) -> list[Project]:
        soup = listing_soup(html)
        cards = soup.select("article")
        if not cards:
            # Zero unknown cards is an error, never a silently successful empty crawl.
            raise SourceError("unrecognized_listing_layout")
        projects, seen, invalid = [], set(), 0
        for card in cards[:100]:
            try:
                link = card.select_one("h2 a[href]")
                if link is None:
                    raise ValueError("missing_link")
                url = safe_url(str(link["href"]), base=self.url, allowed_hosts=self.allowed_hosts)
                match = re.fullmatch(r"/freelance/([^/]+)-\d+/[^/]+-(\d+)/?", urlsplit(url).path)
                if not match:
                    raise ValueError("invalid_project_path")
                heading = link.find_parent("h2")
                # Budget is in the heading row, never extracted from arbitrary description numbers.
                budget_node = heading.parent.select_one("span.whitespace-nowrap") if heading else None
                lower, upper, currency = parse_budget(text_of(budget_node))
                spans = [text_of(n) for n in card.select("span")]
                date = next((s for s in spans if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s)), None)
                responses = next((s for s in spans if re.fullmatch(r"[\d\s]+\s+заяв\w*", s)), None)
                views = next((s for s in spans if re.fullmatch(r"[\d\s]+\s+просмотр\w*", s)), None)
                tags = [text_of(a) for a in card.select("a[href]") if a is not link and re.fullmatch(r"/freelance/[^/]+/", str(a["href"]))]
                category = WEB_CATEGORIES.get(match.group(1), match.group(1))
                project = Project(
                    source=self.name, source_id=match.group(2), title=text_of(link), url=url,
                    description=text_of(card.select_one("p")) or None,
                    published_at=parse_publication_time(date, now), budget_min=lower, budget_max=upper, currency=currency,
                    responses_count=parse_count(responses), views_count=parse_count(views),
                    category=", ".join([category, *tags]),
                )
                if project.key not in seen:
                    projects.append(project)
                    seen.add(project.key)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                invalid += 1
        if invalid:
            self.parse_errors += invalid
            event("invalid_cards", source=self.name, count=invalid)
        if not projects:
            raise SourceError("all_cards_invalid")
        return projects
