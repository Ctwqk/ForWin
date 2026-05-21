from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from forwin.governance import DecisionEventType
from forwin.models.governance import DecisionEvent


class PulpBeatPolicyDecision(BaseModel):
    fatal: bool = False
    reason: str = ""
    consecutive_missing_payoff: int = 0
    threshold: int = 2


def evaluate_pulp_beat_policy(
    *,
    session: Any,
    project_id: str,
    chapter_number: int,
    hard_floor_result: Any,
    config: Any,
) -> PulpBeatPolicyDecision:
    threshold = _payoff_gap_limit(config)
    current_missing = _visible_payoff_missing(_pulp_beat_payload(hard_floor_result))
    if not current_missing:
        return PulpBeatPolicyDecision(threshold=threshold)
    if not _policy_enabled(config):
        return PulpBeatPolicyDecision(
            consecutive_missing_payoff=1,
            threshold=threshold,
        )

    consecutive = 1 + _prior_consecutive_missing(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        limit=max(0, threshold - 1),
    )
    fatal = consecutive >= threshold
    return PulpBeatPolicyDecision(
        fatal=fatal,
        reason="pulp_visible_payoff_consecutive_missing" if fatal else "",
        consecutive_missing_payoff=consecutive,
        threshold=threshold,
    )


def _policy_enabled(config: Any) -> bool:
    profile = str(getattr(config, "quality_profile", "") or "").strip()
    if profile == "pulp":
        return True
    policy = getattr(config, "long_run_policy", None)
    mode = str(getattr(policy, "mode", "") or "").strip()
    return mode in {"factory_batch", "soak_test"}


def _payoff_gap_limit(config: Any) -> int:
    policy = getattr(config, "long_run_policy", None)
    try:
        return max(1, int(getattr(policy, "payoff_gap_limit", 2) or 2))
    except (TypeError, ValueError):
        return 2


def _pulp_beat_payload(hard_floor_result: Any) -> dict[str, Any]:
    metadata = getattr(hard_floor_result, "metadata", {}) or {}
    beat = metadata.get("pulp_beat") if isinstance(metadata, dict) else None
    return beat if isinstance(beat, dict) else {}


def _visible_payoff_missing(payload: dict[str, Any]) -> bool:
    return payload.get("visible_payoff_present") is False


def _prior_consecutive_missing(
    *,
    session: Any,
    project_id: str,
    chapter_number: int,
    limit: int,
) -> int:
    if limit <= 0 or not hasattr(session, "execute"):
        return 0
    rows = (
        session.execute(
            select(DecisionEvent)
            .where(
                DecisionEvent.project_id == project_id,
                DecisionEvent.event_type == DecisionEventType.PULP_BEAT_EVALUATED,
                DecisionEvent.chapter_number < int(chapter_number or 0),
            )
            .order_by(DecisionEvent.chapter_number.desc(), DecisionEvent.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    consecutive = 0
    seen_chapters: set[int] = set()
    expected = int(chapter_number or 0) - 1
    for row in rows:
        row_chapter = int(getattr(row, "chapter_number", 0) or 0)
        if row_chapter in seen_chapters:
            continue
        if row_chapter != expected:
            break
        seen_chapters.add(row_chapter)
        payload = _json_loads(getattr(row, "payload_json", "{}"), {})
        beat = payload.get("pulp_beat") if isinstance(payload, dict) else {}
        if not isinstance(beat, dict) or not _visible_payoff_missing(beat):
            break
        consecutive += 1
        expected -= 1
    return consecutive


def _json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
