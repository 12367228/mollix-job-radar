from datetime import datetime
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from .base import SourceAdapter, listing_soup, text_of
from ..http import SourceError
from ..logging_utils import event
from ..models import Project
from ..safety import closed_status
from ..normalization import parse_budget, parse_count, parse_publication_time, safe_url


class FreelanceRuAdapter(SourceAdapter):
    name = "freelance_ru"
    # Public GET form: c[]=4 selects «Веб-разработка и IT» (verified 2026-09-23).
    url = "https://freelance.ru/task?c%5B%5D=4"
    allowed_hosts = frozenset({"freelance.ru", "www.freelance.ru"})
    max_discovery_pages = 3

    def next_page(self, html, url, page):
        def filters(value):
            return sorted((re.sub(r"\[\d+\]", "[]", k), v) for k, v in parse_qsl(value) if k != "page")
        current = urlsplit(url)
        for link in listing_soup(html).select('a[href][data-page]'):
            try:
                target = safe_url(str(link["href"]), base=url, allowed_hosts=self.allowed_hosts)
                parts = urlsplit(target)
                query = parse_qsl(parts.query)
                if (parts.hostname == current.hostname and parts.path == current.path == "/task"
                        and [v for k, v in query if k == "page"] == [str(page + 1)]
                        and filters(parts.query) == filters(current.query)):
                    return target
            except ValueError:
                continue
        return None

    # q is the site's public GET search field; all these searches were verified live.
    search_terms = ("telegram", "python", "api", "автоматизация", "парсинг", "1с",
                    "javascript", "react", "backend", "devops")

    @property
    def discovery_urls(self) -> list[str]:
        return [self.url, *["https://freelance.ru/task?" + urlencode({"q": term}) for term in self.search_terms]]

    def fetch(self, client, *, now):
        return self.fetch_pages(client, self.discovery_urls, now=now) if self.enabled else []

    def parse(self, html: str, *, now: datetime | None = None) -> list[Project]:
        soup = listing_soup(html)
        cards = soup.select("article.task-card")
        if not cards:
            if soup.select_one(".task-feed-list .empty"):
                return []
            raise SourceError("unrecognized_listing_layout")
        projects, seen = [], set()
        public_cards, invalid = 0, 0
        for card in cards[:100]:
            if card.select_one(".task-badge--premium") or "task-card--premium" in card.get("class", []):
                continue
            public_cards += 1
            try:
                link = card.select_one("a.task-card__title-link[href]")
                if link is None:
                    raise ValueError("missing_link")
                url = safe_url(str(link["href"]), base=self.url, allowed_hosts=self.allowed_hosts)
                match = re.fullmatch(r"/task/view/(\d+)/?", urlsplit(url).path)
                if not match:
                    raise ValueError("invalid_project_path")
                lower, upper, currency = parse_budget(text_of(card.select_one(".task-card__budget")))
                # Preserve public deadline/experience hints as part of description.
                description = text_of(card.select_one(".task-card__desc"))
                extra = text_of(card.select_one(".task-card__meta-rows"))
                experience = [text_of(n) for n in card.select(".task-card__foot-item") if "Опыт:" in text_of(n)]
                description = " ".join(filter(None, [description, extra, *experience]))
                clock = card.select_one(".task-card__foot-item[title] .fa-clock-o")
                published = clock.find_parent(class_="task-card__foot-item") if clock else None
                date = str(published.get("title", "")) if published else ""
                timestamp = parse_publication_time(date, now) if re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", date) else None
                status, status_evidence = "unknown", None
                for label in card.select(".task-card__status, .task-badge, .task-card__foot-item"):
                    evidence = text_of(label)
                    if closed_status(evidence):
                        status, status_evidence = closed_status(evidence), evidence
                        break
                comments = card.select_one(".task-card__foot .fa-comments")
                views = card.select_one(".task-card__foot .fa-eye")
                project = Project(
                    source=self.name, source_id=match.group(1), title=text_of(link), url=url,
                    description=description or None,
                    published_at=timestamp, publication_confirmed=timestamp is not None,
                    publication_evidence=f"task-card clock title: {date} (MSK)" if timestamp else None,
                    open_status=status, status_evidence=status_evidence,
                    budget_min=lower, budget_max=upper, currency=currency,
                    responses_count=parse_count(text_of(comments.parent)) if comments else None,
                    views_count=parse_count(text_of(views.parent)) if views else None,
                    category=" / ".join(text_of(n) for n in card.select(".task-card__chips .task-chip")) or None,
                )
                if project.key not in seen:
                    projects.append(project)
                    seen.add(project.key)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                invalid += 1
        if invalid:
            self.parse_errors += invalid
            event("invalid_cards", source=self.name, count=invalid)
        if public_cards and not projects:
            raise SourceError("all_cards_invalid")
        event("listing_parsed", source=self.name, cards=min(len(cards), 100), public_cards=public_cards,
              premium_skipped=min(len(cards), 100) - public_cards, projects=len(projects))
        return projects
