from abc import ABC, abstractmethod
from datetime import datetime
import re

from bs4 import BeautifulSoup, Tag

from ..http import PublicHTTPClient, SourceError
from ..models import Project
from ..normalization import clean_text


class SourceAdapter(ABC):
    name: str
    url: str
    allowed_hosts: frozenset[str]
    enabled: bool = True
    max_discovery_pages: int = 1

    def __init__(self):
        self.parse_errors = 0
        self.page_errors: dict[str, str] = {}
        self.pages_fetched = 0
        self.raw_records = 0
        self.filtered_count = 0
        self.pagination_skipped: dict[str, str] = {}
        self.detail_errors: dict[str, str] = {}
        self.details_fetched = 0

    def next_page(self, html: str, url: str, page: int) -> str | None:
        return None

    def fetch(self, client: PublicHTTPClient, *, now: datetime) -> list[Project]:
        if not self.enabled:
            return []
        return self.fetch_pages(client, [self.url], now=now)

    def fetch_pages(self, client: PublicHTTPClient, urls: list[str], *, now: datetime) -> list[Project]:
        self.parse_errors, self.pages_fetched, self.raw_records, self.filtered_count = 0, 0, 0, 0
        self.page_errors = {}
        self.pagination_skipped = {}
        projects = []
        queue = [(url, 1) for url in dict.fromkeys(urls)]
        seen_pages, pagination_blocked = set(), False
        for url, page in queue:
            if url in seen_pages or (page > 1 and pagination_blocked):
                continue
            seen_pages.add(url)
            try:
                html = client.get_listing(url, self.allowed_hosts)
            except SourceError as error:
                if page > 1 and str(error) == "robots_disallowed":
                    self.pagination_skipped[url] = str(error)
                    pagination_blocked = True
                    continue
                self.page_errors[url] = str(error)
                # Do not try alternate pages to get around a host's access restriction.
                if str(error) in {"access_restricted", "challenge_page"}:
                    break
                continue
            before = self.parse_errors
            try:
                parsed = self.parse(html, now=now)
            except Exception as error:
                if self.parse_errors == before:
                    self.parse_errors += 1
                code = str(error) if isinstance(error, SourceError) else "unexpected_parse_error"
                self.page_errors[url] = code
                if code == "challenge_page":
                    break
                continue
            self.pages_fetched += 1
            self.raw_records += len(parsed)
            projects.extend(parsed)
            if page < min(3, self.max_discovery_pages) and not pagination_blocked:
                next_url = self.next_page(html, url, page)
                if next_url and next_url not in seen_pages:
                    queue.append((next_url, page + 1))
        if not self.pages_fetched and self.page_errors:
            raise SourceError(next(iter(self.page_errors.values())))
        # Within-source copies from overlapping discovery pages are exact ID/URL copies.
        seen_keys, seen_urls, unique = {}, {}, []
        from ..deduplication import normalized_url
        from ..safety import merge_safety
        for project in projects:
            url = normalized_url(project.url)
            index = seen_keys.get(project.key, seen_urls.get(url))
            if index is None:
                index = len(unique)
                unique.append(project)
            else:
                unique[index] = merge_safety(unique[index], [unique[index], project])
            seen_keys[project.key] = seen_urls[url] = index
        return unique

    @abstractmethod
    def parse(self, html: str, *, now: datetime | None = None) -> list[Project]:
        """Parse a single public listing, raising SourceError if layout is unknown."""


def listing_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    title = text_of(soup.select_one("title"))
    if re.search(r"captcha|just a moment|access denied|доступ ограничен|проверка браузера", title, re.I) or soup.select_one("form[action*='captcha'], #challenge-form"):
        raise SourceError("challenge_page")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    return soup


def text_of(node: Tag | None) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node else ""
