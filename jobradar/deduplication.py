"""Conservative, deterministic deduplication within a discovery batch."""
from collections import defaultdict
from hashlib import sha256
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .normalization import clean_text, safe_url
from .safety import merge_safety


def normalized_title(title: str) -> str:
    return re.sub(r"[^\w]+", " ", clean_text(title).casefold().replace("ё", "е")).strip()


def normalized_url(url: str) -> str:
    p = urlsplit(safe_url(url))
    host = p.hostname.removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    # A title slug or tracking parameter is not part of an exchange's project ID.
    patterns = {"fl.ru": r"(/projects/\d+)(?:/.*)?", "freelance.ru": r"(/task/view/\d+)(?:/)?",
                "freelance.habr.com": r"(/tasks/\d+)(?:/)?"}
    match = re.fullmatch(patterns.get(host, r"(?!)"), path)
    if match:
        return f"https://{host}{match.group(1)}"
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in {"ref", "referrer", "fbclid", "gclid", "yclid"})
    return urlunsplit(("https", host, path, urlencode(query), ""))


def project_link(url: str) -> bool:
    p = urlsplit(normalized_url(url))
    return bool((p.hostname == "fl.ru" and re.fullmatch(r"/projects/\d+", p.path)) or
                (p.hostname == "freelance.ru" and re.fullmatch(r"/task/view/\d+", p.path)) or
                (p.hostname == "freelance.habr.com" and re.fullmatch(r"/tasks/\d+", p.path)) or
                (p.hostname == "t.me" and re.fullmatch(r"/[A-Za-z][A-Za-z0-9_]{4,31}/\d+", p.path)))


def fingerprint(project) -> str:
    target = project.external_url or (project.url if project.origin_kind == "marketplace" else "")
    value = normalized_title(project.title) + "\n" + (normalized_url(target) if target else "")
    return sha256(value.encode("utf-8")).hexdigest()


def deduplicate_projects(projects):
    """Prefer an exchange, then a direct post, then a repost; never copy a repost's date."""
    parents = list(range(len(projects)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def join(a, b):
        parents[root(b)] = root(a)

    keys, urls, fingerprints = {}, {}, {}
    titles = defaultdict(list)
    for i, p in enumerate(projects):
        if p.key in keys:
            join(i, keys[p.key])
        keys[p.key] = i
        fp = fingerprint(p)
        title = normalized_title(p.title)
        if p.external_url or p.origin_kind == "marketplace" or (len(title) >= 24 and len(title.split()) >= 4):
            if fp in fingerprints:
                join(i, fingerprints[fp])
            fingerprints[fp] = i
        identities = {normalized_url(p.url)}
        if p.external_url and project_link(p.external_url):
            identities.add(normalized_url(p.external_url))
        for identity in identities:
            if identity in urls:
                join(i, urls[identity])
            urls[identity] = i
        titles[title].append(i)
    for title, group in titles.items():
        if len(title) < 24 or len(title.split()) < 4:
            continue
        targets = {normalized_url(p.external_url or p.url) for i in group
                   if (p := projects[i]).external_url or p.origin_kind == "marketplace"}
        # A generic title must not merge two different exchange orders/URLs.
        if len(targets) <= 1 and any(projects[i].origin_kind != "marketplace" for i in group):
            for i in group[1:]:
                join(group[0], i)
    groups = defaultdict(list)
    for i, project in enumerate(projects):
        groups[root(i)].append(project)
    priority = {"marketplace": 0, "direct_post": 1, "repost": 2}
    return [merge_safety(min(group, key=lambda p: (priority[p.origin_kind], p.published_at is None,
                                     -len(p.description or ""), p.source, p.source_id)), group) for group in groups.values()]
