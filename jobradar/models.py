from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
import re

from .normalization import clean_text, safe_url


@dataclass(frozen=True)
class Project:
    source: str
    source_id: str
    title: str
    url: str
    description: str | None = None
    published_at: datetime | None = None
    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    currency: str | None = None
    responses_count: int | None = None
    views_count: int | None = None
    category: str | None = None
    channel: str | None = None
    external_url: str | None = None
    origin_kind: str = "marketplace"
    discovery_skip_reason: str | None = None
    publication_confirmed: bool = False
    publication_evidence: str | None = None
    open_status: str = "unknown"
    status_evidence: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", self.source):
            raise ValueError("invalid_source")
        source_id = clean_text(self.source_id, 200)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source_id):
            raise ValueError("invalid_source_id")
        title = clean_text(self.title, 500)
        if not title:
            raise ValueError("missing_title")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "url", safe_url(self.url))
        object.__setattr__(self, "description", clean_text(self.description) or None)
        object.__setattr__(self, "category", clean_text(self.category, 500) or None)
        if self.channel is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", self.channel):
            raise ValueError("invalid_channel")
        if self.external_url is not None:
            object.__setattr__(self, "external_url", safe_url(self.external_url))
        if self.origin_kind not in {"marketplace", "direct_post", "repost"}:
            raise ValueError("invalid_origin_kind")
        if self.discovery_skip_reason not in {None, "advertisement", "education", "full_time_vacancy"}:
            raise ValueError("invalid_discovery_skip_reason")
        if type(self.publication_confirmed) is not bool:
            raise ValueError("invalid_publication_confirmation")
        if self.open_status not in {"unknown", "open", "closed", "awarded", "not_accepting"}:
            raise ValueError("invalid_open_status")
        for field in ("publication_evidence", "status_evidence"):
            object.__setattr__(self, field, clean_text(getattr(self, field), 500) or None)
        if self.published_at:
            if self.published_at.tzinfo is None:
                raise ValueError("published_at_must_include_timezone")
            object.__setattr__(self, "published_at", self.published_at.astimezone(timezone.utc))
        for field in ("budget_min", "budget_max"):
            value = getattr(self, field)
            if value is not None:
                value = Decimal(str(value))
                if not value.is_finite() or not 0 <= value <= Decimal("1000000000000"):
                    raise ValueError("invalid_budget")
                object.__setattr__(self, field, value)
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError("invalid_budget_range")
        if self.currency is not None:
            currency = self.currency.upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("invalid_currency")
            object.__setattr__(self, "currency", currency)
        for field in ("responses_count", "views_count"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid_count")

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def direct_url(self) -> str | None:
        from .safety import direct_project_url
        return direct_project_url(self)

    def to_dict(self) -> dict:
        from .deduplication import fingerprint
        data = asdict(self)
        data["fingerprint"] = fingerprint(self)
        data["direct_url"] = self.direct_url
        data["published_at"] = self.published_at.isoformat() if self.published_at else None
        for field in ("budget_min", "budget_max"):
            data[field] = str(data[field]) if data[field] is not None else None
        return data
