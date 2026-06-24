from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EDUCATION_GENRE = "education_guide"
FALLBACK_GENRE = "media_only_unknown"
AXIS_COLUMNS = ("difficulty_score", "promo_score", "opinion_score", "event_score")
READY_DRAFT_STATUS = "ready_for_publication"


@dataclass(frozen=True)
class PipelineEligibility:
    is_eligible: bool
    reason: str


def clamp_axis(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, number))


def axes_from_label_scores(label_scores: dict[str, Any] | None) -> dict[str, int]:
    scores = label_scores or {}
    nested = scores.get("axes") if isinstance(scores.get("axes"), dict) else {}
    return {axis: clamp_axis(nested.get(axis, scores.get(axis, scores.get(f"axis:{axis}", 0)))) for axis in AXIS_COLUMNS}


def genre_set(primary: str | None, secondary: list[str] | None) -> list[str]:
    values: list[str] = []
    for value in [primary, *(secondary or [])]:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def education_eligibility(
    primary: str | None,
    secondary: list[str] | None,
    axes: dict[str, Any],
    *,
    needs_review: bool = False,
) -> PipelineEligibility:
    genres = genre_set(primary, secondary)
    difficulty = clamp_axis(axes.get("difficulty_score"))
    promo = clamp_axis(axes.get("promo_score"))
    event = clamp_axis(axes.get("event_score"))
    if needs_review:
        return PipelineEligibility(False, "classification_needs_review")
    if primary == FALLBACK_GENRE:
        return PipelineEligibility(False, "media_only_unknown")
    if EDUCATION_GENRE not in genres:
        return PipelineEligibility(False, "education_guide_missing")
    if difficulty < 2:
        return PipelineEligibility(False, "difficulty_below_2")
    if promo != 0:
        return PipelineEligibility(False, "promo_not_zero")
    if event != 0:
        return PipelineEligibility(False, "event_not_zero")
    return PipelineEligibility(True, "education_guide difficulty>=2 promo=0 event=0")


def status_from_state(
    *,
    publication_allowed: bool,
    is_eligible: bool,
    draft_status: str | None,
    has_published_post: bool,
    has_error: bool = False,
) -> str:
    if has_published_post:
        return "published"
    if not publication_allowed:
        return "blocked"
    if not is_eligible:
        return "ineligible"
    if has_error:
        return "rewrite_failed"
    if draft_status == READY_DRAFT_STATUS:
        return READY_DRAFT_STATUS
    if draft_status in {"needs_review", "draft", "approved", "rejected"}:
        return draft_status
    return "rewrite_pending"

