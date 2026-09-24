"""Fail-closed publication and original-card checks, independent of scoring."""
import re
from dataclasses import replace
from urllib.parse import urlsplit

from .normalization import clean_text, safe_url


CARD_PATTERNS = {
    "fl_ru": ("fl.ru", r"/projects/(\d+)/[^/]+\.html"),
    "freelance_ru": ("freelance.ru", r"/task/view/(\d+)/?"),
    "habr_freelance": ("freelance.habr.com", r"/tasks/(\d+)/?"),
    "weblancer": ("weblancer.net", r"/freelance/[^/]+-\d+/[^/]+-(\d+)/?"),
}


def open_status_confirmed(project) -> bool:
    """Only an affirmative status with evidence recorded by the source adapter."""
    return project.open_status == "open" and bool((project.status_evidence or "").strip())


def direct_project_url(project) -> str | None:
    """Only original marketplace cards; a channel/search URL is not a card."""
    target = project.external_url if project.origin_kind != "marketplace" else project.url
    if not target:
        return None
    try:
        url = safe_url(target)
    except ValueError:
        return None
    parts = urlsplit(url)
    for source, (host, pattern) in CARD_PATTERNS.items():
        match = re.fullmatch(pattern, parts.path)
        if parts.hostname.removeprefix("www.") == host and match:
            if project.origin_kind == "marketplace" and (source != project.source or match.group(1) != project.source_id):
                return None
            # Card identity is the path; never pass query parameters to notifications.
            return f"https://{parts.hostname}{parts.path}"
    return None


def closed_status(value: str) -> str | None:
    """Apply only to status labels, never to a project brief."""
    text = clean_text(value).lower().replace("ё", "е").strip(" .!:")
    if re.fullmatch(r"(?:исполнитель|подрядчик)(?: уже)? (?:выбран|определен)|выбран исполнитель", text):
        return "awarded"
    if re.fullmatch(r"(?:(?:проект|заказ|задание) (?:уже )?)?закрыт[оа]?|завершен[оа]?", text):
        return "closed"
    if re.fullmatch(r"(?:отклики|заявки|предложения)(?: больше)? не принимаются|"
                    r"прием (?:откликов|заявок|предложений) (?:закрыт|завершен|прекращен)", text):
        return "not_accepting"
    return None


def merge_safety(primary, copies):
    """Do not lose an original source's closed status when collapsing duplicate cards."""
    originals = [p for p in copies if p.origin_kind == "marketplace"]
    closed = next((p for p in originals if p.open_status in {"closed", "awarded", "not_accepting"}), None)
    if closed:
        primary = replace(primary, open_status=closed.open_status, status_evidence=closed.status_evidence)
    dates = {p.published_at for p in originals if p.publication_confirmed}
    if len(dates) > 1:
        primary = replace(primary, publication_confirmed=False, publication_evidence="conflicting original publication fields")
    return primary
