"""Bounded public GETs with the standard HTTPX network environment and TLS."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
import time
from urllib.parse import urlsplit

import httpx

from .normalization import safe_url

USER_AGENT = "MollixJobRadar/0.1 (+public freelance listings; no login)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def create_http_client() -> httpx.Client:
    # Keep HTTPX defaults for proxies, CA certificates, TLS verification and transport.
    # Disabling trust_env breaks networks which require their configured proxy.
    return httpx.Client(timeout=20)


class SourceError(RuntimeError):
    """Messages are fixed codes, safe to log without leaking remote content."""


def retry_delay(value: str | None, fallback: float) -> float:
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return fallback


class RobotsPolicy:
    """Allow/Disallow wildcard and longest-match rules for this bot (RFC 9309)."""

    def __init__(self, text: str, user_agent: str = USER_AGENT):
        groups: list[tuple[list[str], list[tuple[str, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[str, str]] = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if rules:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif agents and key in {"allow", "disallow", "crawl-delay", "request-rate"}:
                rules.append((key, value))
        if agents:
            groups.append((agents, rules))
        bot = user_agent.split("/", 1)[0].lower()
        ranked = [(max((len(a) if a != "*" else 0 for a in aa if a == "*" or a in bot), default=-1), rr) for aa, rr in groups]
        best = max((rank for rank, _ in ranked), default=-1)
        self.rules = [rule for rank, rr in ranked if rank == best and rank >= 0 for rule in rr]
        self.delay = 2.0
        for key, value in self.rules:
            try:
                if key == "crawl-delay":
                    self.delay = max(self.delay, float(value))
                elif key == "request-rate":
                    requests, seconds = value.split("/", 1)
                    self.delay = max(self.delay, float(seconds) / float(requests))
            except (ValueError, ZeroDivisionError):
                continue

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        matches = []
        for key, value in self.rules:
            if key not in {"allow", "disallow"} or not value:
                continue
            end = value.endswith("$")
            pattern = re.escape(value[:-1] if end else value).replace(r"\*", ".*")
            if re.match("^" + pattern + ("$" if end else ""), path):
                matches.append((len(value.replace("*", "").rstrip("$")), key == "allow"))
        return max(matches)[1] if matches else True


class PublicHTTPClient:
    def __init__(self, *, client: httpx.Client | None = None, sleep=time.sleep, clock=time.monotonic, max_bytes: int = MAX_RESPONSE_BYTES):
        self.client = client if client is not None else create_http_client()
        self.owns_client = client is None
        self.sleep, self.clock, self.max_bytes = sleep, clock, max_bytes
        self.last_request: dict[str, float] = {}
        self.policies: dict[str, RobotsPolicy] = {}

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def _get(self, url: str, hosts: frozenset[str], *, robots: bool = False, delay: float = 2) -> str:
        safe_url(url, allowed_hosts=hosts)
        host = urlsplit(url).hostname or ""
        if delay > 60:
            raise SourceError("crawl_delay_exceeds_run_budget")
        for attempt in range(3):
            previous = self.last_request.get(host)
            if previous is not None:
                self.sleep(max(0, delay - (self.clock() - previous)))
            self.last_request[host] = self.clock()
            self.client.cookies.clear()
            try:
                with self.client.stream("GET", url, follow_redirects=False, headers={
                    "User-Agent": USER_AGENT, "Accept": "text/plain" if robots else "text/html", "Accept-Encoding": "identity",
                }) as response:
                    if robots and response.status_code in (404, 410):
                        return ""
                    if response.status_code in {429, 500, 502, 503, 504}:
                        pause = retry_delay(response.headers.get("Retry-After"), 2 ** attempt)
                        if attempt == 2 or pause > 30:
                            raise SourceError("temporary_http_error")
                        self.sleep(pause)
                        continue
                    if response.status_code in (401, 403):
                        raise SourceError("access_restricted")
                    if response.status_code != 200:
                        raise SourceError("unexpected_http_status")
                    content_type = response.headers.get("Content-Type", "").lower()
                    if not any(t in content_type for t in (("text/plain", "text/text") if robots else ("text/html", "application/xhtml+xml"))):
                        raise SourceError("unexpected_content_type")
                    cap = min(self.max_bytes, 262144) if robots else self.max_bytes
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > cap:
                        raise SourceError("response_too_large")
                    chunks, size, started = [], 0, self.clock()
                    for chunk in response.iter_bytes(chunk_size=16384):
                        size += len(chunk)
                        if size > cap:
                            raise SourceError("response_too_large")
                        if self.clock() - started > 45:
                            raise SourceError("response_deadline_exceeded")
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8", errors="replace")
            except httpx.RequestError:
                if attempt == 2:
                    raise SourceError("network_error") from None
                self.sleep(2 ** attempt)
        raise SourceError("request_failed")

    def get_listing(self, url: str, allowed_hosts: frozenset[str]) -> str:
        safe_url(url, allowed_hosts=allowed_hosts)
        parts = urlsplit(url)
        origin = f"https://{parts.hostname}"
        if origin not in self.policies:
            robots = self._get(origin + "/robots.txt", allowed_hosts, robots=True)
            self.policies[origin] = RobotsPolicy(robots)
        policy = self.policies[origin]
        if not policy.allowed(url):
            raise SourceError("robots_disallowed")
        return self._get(url, allowed_hosts, delay=policy.delay)
