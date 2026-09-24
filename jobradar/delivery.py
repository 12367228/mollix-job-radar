"""Pure, fail-closed quality policy; no network calls or state writes."""
from dataclasses import replace
from typing import TYPE_CHECKING

from .models import Project
from .safety import open_status_confirmed
from .stack import HIGH_MATCH, PARTIAL_MATCH

if TYPE_CHECKING:
    from .scoring import Assessment


def apply_delivery_gate(project: Project, assessment: "Assessment", *, min_score: int = 7,
                        max_responses: int = 20, allow_stack_mismatch: bool = False,
                        duplicate: bool = False) -> "Assessment":
    """All gates compose. A budget exception never bypasses score or configured limits."""
    blockers = []
    if not assessment.eligible:
        blockers.append("INELIGIBLE")
    if assessment.score < min_score:
        blockers.append("MIN_SCORE")
    mismatch = (assessment.stack_compatibility not in {HIGH_MATCH, PARTIAL_MATCH}
                or "STACK_MISMATCH" in assessment.risk_flags)
    if mismatch and not allow_stack_mismatch:
        blockers.append("STACK_MISMATCH")
    responses = project.responses_count
    if responses is None:
        blockers.append("RESPONSES_UNKNOWN")
    else:
        if responses > max_responses:
            blockers.append("COMPETITION")
        # A stated lower bound must qualify; a 20–60k range is not a 50k+ offer.
        budget = project.budget_min if project.budget_min is not None else project.budget_max
        exception = (assessment.stack_compatibility == HIGH_MATCH and not mismatch
                     and project.currency == "RUB" and budget is not None and budget >= 50000)
        if responses > 50 and not exception:
            blockers.append("COMPETITION_OVER_50")
    if assessment.label == "FIRE":
        if assessment.stack_compatibility != HIGH_MATCH or mismatch:
            blockers.append("FIRE_REQUIRES_HIGH_MATCH")
        if not open_status_confirmed(project):
            blockers.append("OPEN_STATUS_UNCONFIRMED")
        if responses is None or responses > 20:
            blockers.append("FIRE_COMPETITION")
    if duplicate:
        blockers.append("ALREADY_SEEN")
    return replace(assessment, delivery_blockers=tuple(blockers))
