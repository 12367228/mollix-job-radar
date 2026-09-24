from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import time

from .config import Settings
from .deduplication import deduplicate_projects
from .delivery import apply_delivery_gate
from .http import PublicHTTPClient, SourceError
from .logging_utils import event
from .models import Project
from .scoring import Assessment, assess_project
from .sources.base import SourceAdapter
from .state import StateStore
from .telegram import TelegramClient, TelegramError, format_message


@dataclass
class RunResult:
    fetched: int = 0
    checked: int = 0
    relevance_pass: int = 0
    fresh: int = 0
    eligible: int = 0
    duplicates: int = 0
    sent: int = 0
    source_errors: list[str] = field(default_factory=list)
    source_error_codes: dict[str, str] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    source_metrics: dict[str, dict] = field(default_factory=dict)
    delivery_error: str | None = None
    candidates: list[tuple[Project, Assessment, bool]] = field(default_factory=list)
    checked_at: datetime | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.delivery_error else 0

    def summary(self) -> dict:
        return {"total_fetched": self.fetched, "unique": self.checked, "fresh": self.fresh,
                "fetched": self.fetched, "checked": self.checked, "relevance_pass": self.relevance_pass,
                "eligible": self.eligible, "FIRE": sum(a.label == "FIRE" for _, a, _ in self.candidates),
                "GOOD": sum(a.label == "GOOD" for _, a, _ in self.candidates),
                "telegram_deliverable": sum(a.telegram_deliverable for _, a, _ in self.candidates),
                # Independent counters among eligible unique candidates; may overlap.
                # Unknown status blocks FIRE, but can still allow GOOD 7 delivery.
                "filtered_by_competition": sum(a.eligible and bool(set(a.delivery_blockers) & {
                    "COMPETITION", "COMPETITION_OVER_50", "RESPONSES_UNKNOWN"}) for _, a, _ in self.candidates),
                "filtered_by_stack": sum(a.eligible and "STACK_MISMATCH" in a.delivery_blockers for _, a, _ in self.candidates),
                "filtered_by_open_status": sum(a.eligible and "OPEN_STATUS_UNKNOWN" in a.risk_flags for _, a, _ in self.candidates),
                "duplicates": self.duplicates, "sent": self.sent, "source_counts": self.source_counts,
                "source_metrics": self.source_metrics, "discovery_duplicates": self.fetched - self.checked,
                "source_errors": self.source_errors, "source_error_codes": self.source_error_codes,
                "delivery_error": self.delivery_error,
                "checked_at": self.checked_at.isoformat() if self.checked_at else None}


def candidate_order(item):
    project, assessment, _ = item
    return (-assessment.score, assessment.age_hours if assessment.age_hours is not None else float("inf"),
            project.responses_count if project.responses_count is not None else float("inf"), project.key)


def execute(settings: Settings, adapters: list[SourceAdapter], http: PublicHTTPClient | None, state: StateStore,
            *, telegram: TelegramClient | None = None, fixtures: Path | None = None,
            now: datetime | None = None, sleep=time.sleep, clock=None) -> RunResult:
    fixed_now = now
    current_time = clock or (lambda: fixed_now or datetime.now(timezone.utc))
    now = current_time()
    result = RunResult()

    def assess_candidate(project, *, now, duplicate=False):
        assessment = assess_project(project, now=now, max_project_age_hours=settings.max_project_age_hours,
                                    hot_project_age_hours=settings.hot_project_age_hours)
        return apply_delivery_gate(project, assessment, min_score=settings.telegram_min_score,
                                   max_responses=settings.telegram_max_responses,
                                   allow_stack_mismatch=settings.telegram_allow_stack_mismatch, duplicate=duplicate)

    discovered = []
    for adapter in adapters:
        metrics = {"status": "disabled" if not getattr(adapter, "enabled", True) else "ok",
                   "fetched": 0, "parse_errors": 0, "fresh_count": 0, "relevant_count": 0,
                   "eligible_count": 0, "pages_fetched": 0, "page_errors": {}, "filtered_count": 0}
        result.source_metrics[adapter.name] = metrics
        if metrics["status"] == "disabled":
            continue
        try:
            if fixtures:
                projects = adapter.parse((fixtures / f"{adapter.name}.html").read_text(encoding="utf-8"), now=now)
            else:
                if http is None:
                    raise SourceError("missing_http_client")
                projects = adapter.fetch(http, now=now)
            event("source_ok", source=adapter.name, projects=len(projects))
            result.source_counts[adapter.name] = len(projects)
            result.fetched += len(projects)
        except Exception as error:
            # An unexpected parser bug still cannot take down the other source.
            result.source_errors.append(adapter.name)
            code = str(error) if isinstance(error, SourceError) else "unexpected_source_error"
            result.source_error_codes[adapter.name] = code
            parse_errors = getattr(adapter, "parse_errors", 0)
            if code in {"unrecognized_listing_layout", "all_cards_invalid", "unexpected_parse_error"}:
                parse_errors = max(1, parse_errors)
            metrics.update(status="error", parse_errors=parse_errors,
                           page_errors=getattr(adapter, "page_errors", {}))
            event("source_failed", source=adapter.name, code=code)
            continue
        page_errors = getattr(adapter, "page_errors", {})
        metrics.update(fetched=len(projects), parse_errors=getattr(adapter, "parse_errors", 0),
                       pages_fetched=1 if fixtures else getattr(adapter, "pages_fetched", 1),
                       page_errors=page_errors, filtered_count=getattr(adapter, "filtered_count", 0),
                       pagination_skipped=getattr(adapter, "pagination_skipped", {}),
                       details_fetched=getattr(adapter, "details_fetched", 0), detail_errors=getattr(adapter, "detail_errors", {}))
        detail_errors = getattr(adapter, "detail_errors", {})
        if page_errors or detail_errors:
            metrics["status"] = "partial"
            result.source_errors.append(adapter.name)
            result.source_error_codes[adapter.name] = next(iter((page_errors or detail_errors).values()))
            event("source_partial", source=adapter.name, errors=len(page_errors) + len(detail_errors))
        discovered.extend(projects)
    # Use one fresh timestamp after discovery, not the start of a potentially slow fetch.
    now = current_time()
    result.checked_at = now
    for metrics in result.source_metrics.values():
        metrics.update(fresh_count=0, relevant_count=0, eligible_count=0)
    for project in discovered:
        assessment = assess_project(project, now=now, max_project_age_hours=settings.max_project_age_hours,
                                    hot_project_age_hours=settings.hot_project_age_hours)
        # Adapters normally share their name with Project.source; test sources may not.
        metrics = result.source_metrics.get(project.source)
        if metrics is not None:
            metrics["fresh_count"] += int(project.publication_confirmed and assessment.age_hours is not None
                                           and 0 <= assessment.age_hours <= min(24, settings.max_project_age_hours))
            metrics["relevant_count"] += int(assessment.relevance_gate)
            metrics["eligible_count"] += int(assessment.eligible)
    for project in deduplicate_projects(discovered):
        duplicate = state.contains(project)
        assessment = assess_candidate(project, now=now, duplicate=duplicate)
        result.candidates.append((project, assessment, duplicate))
        result.checked += 1
        result.fresh += int(project.publication_confirmed and assessment.age_hours is not None
                            and 0 <= assessment.age_hours <= min(24, settings.max_project_age_hours))
        result.relevance_pass += int(assessment.relevance_gate)
        result.eligible += int(assessment.eligible)
        result.duplicates += int(duplicate)
    result.candidates.sort(key=candidate_order)
    if telegram is not None:
        for project in discovered:
            state.observe(project, now=now)
        state.save()
        for project, assessment, duplicate in result.candidates:
            if duplicate or not assessment.telegram_deliverable or result.sent >= settings.max_send:
                continue
            assessment = assess_candidate(project, now=current_time(), duplicate=state.contains(project))
            if not assessment.telegram_deliverable:
                continue
            # Build the message before reserving; formatter errors must not reserve a job.
            message = format_message(project, assessment)
            if not state.reserve(project, assessment, now=current_time()):
                continue
            try:
                telegram.send_message(message)
            except TelegramError as error:
                if not error.uncertain:
                    state.release(project)
                result.delivery_error = str(error)
                event("delivery_failed", code=str(error), uncertain=error.uncertain)
                break
            state.mark_sent(project, now=current_time())
            result.sent += 1
            event("project_sent", source=project.source, source_id=project.source_id, score=assessment.score)
            sleep(1.1)
    event("run_finished", **result.summary(), dry_run=telegram is None)
    return result
