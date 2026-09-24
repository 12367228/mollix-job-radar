from dataclasses import replace
from datetime import datetime
import re
from urllib.parse import urlsplit
from bs4 import Comment

from .base import SourceAdapter, listing_soup, text_of
from ..http import SourceError
from ..models import Project
from ..safety import closed_status
from ..scoring import assess_project
from ..normalization import parse_budget, parse_count, parse_publication_time, safe_url


class FlRuAdapter(SourceAdapter):
    """Public, server-rendered project listings; no login or internal APIs."""
    name = "fl_ru"
    url = "https://www.fl.ru/projects/"
    allowed_hosts = frozenset({"www.fl.ru", "fl.ru"})
    max_discovery_pages = 3
    max_detail_pages = 20
    # Observed links in /projects/ and verified without authentication on 2026-09-23.
    discovery_urls = [
        "https://www.fl.ru/projects/category/programmirovanie/",
        "https://www.fl.ru/projects/category/saity/",
        "https://www.fl.ru/projects/category/avtomatizaciya-biznesa/",
    ]

    def fetch(self, client, *, now):
        if not self.enabled:
            return []
        projects = self.fetch_pages(client, self.discovery_urls, now=now)
        self.detail_errors, self.details_fetched = {}, 0
        if any(code in {"access_restricted", "challenge_page"} for code in self.page_errors.values()):
            return projects
        # Listing ages may reflect rounding/raising. Only the original card's
        # explicit publication field can unlock delivery. Bound extra GETs.
        candidates = []
        for i, project in enumerate(projects):
            provisional = replace(project, publication_confirmed=True,
                                  publication_evidence="listing estimate, awaiting original card")
            assessment = assess_project(provisional, now=now)
            if assessment.eligible:
                candidates.append((i, assessment.score, assessment.age_hours))
        candidates.sort(key=lambda item: (-item[1], item[2]))
        for i, _, _ in candidates[:self.max_detail_pages]:
            project = projects[i]
            try:
                html = client.get_listing(project.url, self.allowed_hosts)
                projects[i] = self.confirm_card(project, html, now=now)
                self.details_fetched += 1
                if not projects[i].publication_confirmed:
                    self.detail_errors[project.key] = "unconfirmed_original_publication"
            except SourceError as error:
                self.detail_errors[project.key] = str(error)
                if str(error) in {"access_restricted", "challenge_page"}:
                    break
        return projects

    def next_page(self, html, url, page):
        current = urlsplit(url)
        base_path = re.sub(r"page-\d+/$", "", current.path)
        for link in listing_soup(html).select('a[href]'):
            try:
                target = safe_url(str(link["href"]), base=url, allowed_hosts=self.allowed_hosts)
                parts = urlsplit(target)
                if (parts.hostname == current.hostname and not parts.query
                        and parts.path == f"{base_path}page-{page + 1}/"):
                    return target
            except ValueError:
                continue
        return None

    def confirm_card(self, project, html, *, now):
        soup = listing_soup(html)
        brief = soup.select_one(f"#projectp{project.source_id}")
        if brief is None:
            raise SourceError("unrecognized_project_card")
        dates = []
        for node in soup.select(".text-10.opacity-50"):
            match = re.fullmatch(
                r"Опубликован\s+(\d{2}\.\d{2}\.\d{4})\s+в\s+(\d{2}:\d{2})"
                r"(?:\s+Последнее изменение:\s+\d{2}\.\d{2}\.\d{4}\s+в\s+\d{2}:\d{2})?", text_of(node))
            if match:
                dates.append(" ".join(match.groups()))
        timestamp = parse_publication_time(dates[0], now) if len(dates) == 1 else None
        status, evidence = project.open_status, project.status_evidence
        for node in soup.select('[data-id="qa-order-status"], .fl-project-status, .b-status'):
            value = text_of(node)
            if closed_status(value):
                status, evidence = closed_status(value), value
                break
        # Exclude the customer's brief and replies before reading standalone status labels.
        for node in soup.select('.fl-project-content__description-text, [id^="projectp"], .b-post__txt, h1, h2, .comment, .b-comment'):
            node.decompose()
        for node in soup.find_all(string=True):
            if isinstance(node, Comment):
                continue
            value = str(node).strip()
            if closed_status(value):
                status, evidence = closed_status(value), value
                break
        else:
            offer = soup.select_one("#reply_offer")
            # A hidden/disabled offer control is not evidence of accepting replies.
            available = offer is not None and all(
                not (node.has_attr("disabled") or node.has_attr("hidden")
                     or node.get("aria-disabled", "").lower() == "true"
                     or node.get("aria-hidden", "").lower() == "true"
                     or set(node.get("class", [])) & {"disabled", "hidden", "d-none"}
                     or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", node.get("style", ""), re.I))
                for node in [offer, *offer.parents])
            if status == "unknown" and available:
                status, evidence = "open", "original card: reply_offer"
        return replace(project, published_at=timestamp, publication_confirmed=timestamp is not None,
                       publication_evidence=f"original card: Опубликован {dates[0]} (MSK)" if timestamp else None,
                       open_status=status, status_evidence=evidence)

    def parse(self, html: str, *, now: datetime | None = None) -> list[Project]:
        filtered_before = self.filtered_count
        soup = listing_soup(html)
        cards = soup.select('[id^="project-item"]')
        if not cards:
            raise SourceError("unrecognized_listing_layout")
        heading = text_of(soup.select_one("h1"))
        category = ("Программирование" if "программ" in heading.lower() else
                    "Web development" if "сайт" in heading.lower() else
                    "Автоматизация" if "автоматизац" in heading.lower() else None)
        projects = []
        for card in cards[:100]:
            kind = text_of(card.select_one(".b-post__foot .b-post__bold"))
            if re.search(r"вакансия|конкурс", kind, re.I):
                self.filtered_count += 1
                continue
            try:
                link = card.select_one("h2 a[href]")
                if not link:
                    raise ValueError("missing_title_link")
                url = safe_url(str(link["href"]), base=self.url, allowed_hosts=self.allowed_hosts)
                match = re.fullmatch(r"/projects/(\d+)/[^/]+\.html", urlsplit(url).path)
                if not match:
                    raise ValueError("invalid_project_link")
                lower, upper, currency = parse_budget(text_of(card.select_one(".b-post__price")))
                date = text_of(card.select_one(".b-post__foot span.text-gray-opacity-4"))
                status_label = text_of(card.select_one('[data-id="qa-order-status"]'))
                status = closed_status(status_label) or "unknown"
                responses = card.select_one('[data-id="fl-view-count-href"]')
                response_text = text_of(responses)
                if not response_text:
                    icon = card.select_one('[title="Количество ответов"]')
                    response_text = text_of(icon.parent) if icon else ""
                projects.append(Project(
                    source=self.name, source_id=match.group(1), title=text_of(link), url=url,
                    description=text_of(card.select_one(".b-post__body .b-post__txt")) or None,
                    published_at=parse_publication_time(date, now), budget_min=lower, budget_max=upper,
                    currency=currency, responses_count=parse_count(response_text),
                    publication_evidence=f"unconfirmed listing age: {date}" if date else None,
                    open_status=status, status_evidence=status_label or None,
                    # Paid pinned cards may be shown outside their own category.
                    category=None if "topprjpay" in card.get("class", []) else category,
                ))
            except (ValueError, TypeError, KeyError, ArithmeticError):
                self.parse_errors += 1
        if not projects and self.filtered_count == filtered_before:
            raise SourceError("all_cards_invalid")
        return projects
