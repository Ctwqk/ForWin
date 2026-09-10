"""Phase B of the Audience Feedback Layer."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.models import (
    FeedbackActionRecord,
    PublisherCommentSyncJob,
    PublisherRawComment,
    ReaderScaleSnapshot,
    SignalWindowAggregate,
    new_id,
)
from forwin.protocol import AudienceTrendView

logger = logging.getLogger(__name__)

_SIGNAL_LEVEL_ORDER = {
    "noise": 0,
    "candidate": 1,
    "watchlist": 2,
    "confirmed": 3,
}
_SCORE_LEVEL_WEIGHT = {
    "noise": 0.1,
    "candidate": 0.4,
    "watchlist": 0.7,
    "confirmed": 1.0,
}
_POSITIVE_COMMENT_KEYWORDS = ("喜欢", "精彩", "好看", "期待", "爽", "牛", "神")
_NEGATIVE_COMMENT_KEYWORDS = ("水", "拖", "崩", "失望", "弃", "烂", "短", "乱")
_QUESTION_COMMENT_KEYWORDS = ("为什么", "怎么", "是不是", "会不会", "求", "能不能")

# ── Reader-scale tiers (v2.6 spec §9.2) ─────────────────────────────

_READER_TIERS = [
    (0, 50),       # Tier 0: pre-launch
    (50, 300),     # Tier 1: launch
    (300, 2_000),  # Tier 2: early growth
    (2_000, 20_000),  # Tier 3: growth
    (20_000, 10**9),  # Tier 4: mature
]


def reader_tier_for_count(reader_estimate: int) -> int:
    for tier, (lower, upper) in enumerate(_READER_TIERS):
        if lower <= reader_estimate < upper:
            return tier
    return 4


# ── Window definitions ───────────────────────────────────────────────

@dataclass(slots=True)
class WindowDef:
    name: str
    size: int  # chapters back from current


WINDOWS = [
    WindowDef("short", 3),
    WindowDef("medium", 8),
    WindowDef("long", 20),
]


@dataclass(slots=True)
class ReaderScaleEstimate:
    reader_estimate: int
    estimation_method: str
    confidence: float


_PLATFORM_METRIC_FIELDS: dict[str, tuple[str, float]] = {
    "read_count": ("read_count", 0.9),
    "readCount": ("read_count", 0.9),
    "read_num": ("read_count", 0.9),
    "readNum": ("read_count", 0.9),
    "view_count": ("view_count", 0.85),
    "viewCount": ("view_count", 0.85),
    "views": ("view_count", 0.85),
    "pv": ("view_count", 0.8),
    "follower_count": ("follower_count", 0.7),
    "follow_count": ("follower_count", 0.7),
    "favorite_count": ("favorite_count", 0.68),
    "collect_count": ("favorite_count", 0.68),
    "bookshelf_count": ("favorite_count", 0.68),
    "chase_count": ("chase_count", 0.72),
    "interaction_count": ("interaction_count", 0.58),
}


def classify_signal_level(
    *,
    unique_users: int,
    spans_chapters: int,
    severity: int,
    signal_type: str,
) -> str:
    if signal_type == "risk" and severity >= 3:
        return "watchlist" if unique_users < 2 else "confirmed"
    if unique_users < 2:
        return "noise"
    if spans_chapters < 2 and signal_type in (
        "character_heat",
        "relationship_interest",
        "prediction",
    ):
        return "noise"
    if unique_users >= 3 and spans_chapters >= 2:
        return "confirmed"
    return "candidate"


def keyword_dominant_sentiment(comments: Sequence[PublisherRawComment]) -> str:
    positive = 0
    negative = 0
    curious = 0
    for comment in comments:
        text = str(comment.body_text or "")
        if any(keyword in text for keyword in _POSITIVE_COMMENT_KEYWORDS):
            positive += 1
        if any(keyword in text for keyword in _NEGATIVE_COMMENT_KEYWORDS):
            negative += 1
        if any(keyword in text for keyword in _QUESTION_COMMENT_KEYWORDS):
            curious += 1
    if negative > max(positive, curious):
        return "negative"
    if positive > max(negative, curious):
        return "positive"
    if curious:
        return "curious"
    return "neutral"


def keyword_feedback_summary(comment_count: int, dominant_sentiment: str) -> str:
    summary_parts = [f"最近 {comment_count} 条评论"]
    if dominant_sentiment == "negative":
        summary_parts.append("整体情绪偏担忧")
    elif dominant_sentiment == "positive":
        summary_parts.append("整体情绪偏积极")
    elif dominant_sentiment == "curious":
        summary_parts.append("读者对悬念追问较多")
    else:
        summary_parts.append("暂无明确结构化信号")
    return "，".join(summary_parts) + "。"


def score_signal_aggregate_v1(aggregate: SignalWindowAggregate) -> float:
    """Compute the Phase C score_v1 on a normalized 0-1 scale.

    The score intentionally blends the four persisted Phase B counters with
    severity / level confidence so downstream modules can rank stronger signals
    without hard-coding one-off threshold ladders everywhere.
    """
    total_comments = max(1, int(aggregate.total_comment_count or 0))
    reader_estimate = max(1, int(aggregate.reader_estimate or 0))
    prevalence = min(1.0, float(aggregate.hit_comment_count or 0) / float(total_comments))
    penetration = min(
        1.0,
        float(aggregate.unique_user_count or 0) / max(1.0, float(reader_estimate) / 50.0),
    )
    severity = min(1.0, max(0.0, float(aggregate.max_severity or 0) / 3.0))
    confidence = min(1.0, max(0.0, float(aggregate.avg_confidence or 0.0)))
    level_weight = _SCORE_LEVEL_WEIGHT.get(str(aggregate.signal_level or "noise"), 0.1)
    score = (
        0.32 * prevalence
        + 0.26 * penetration
        + 0.18 * severity
        + 0.14 * level_weight
        + 0.10 * confidence
    )
    return round(min(1.0, max(0.0, score)), 4)


def derive_audience_trends(
    aggregates: Sequence[SignalWindowAggregate],
    *,
    window_type: str = "long",
    limit: int = 6,
    min_delta: float = 0.05,
) -> list[AudienceTrendView]:
    """Build a lightweight explicit trend layer from aggregate snapshots."""
    filtered = [
        row
        for row in aggregates
        if (not window_type or row.window_type == window_type)
        and str(row.signal_level or "noise") != "noise"
    ]
    by_key: dict[str, list[SignalWindowAggregate]] = defaultdict(list)
    for row in filtered:
        by_key[str(row.signal_key or "")].append(row)

    trends: list[AudienceTrendView] = []
    for signal_key, rows in by_key.items():
        ordered = sorted(
            rows,
            key=lambda row: (
                int(row.window_chapter_end or 0),
                int(row.window_chapter_start or 0),
                int(row.unique_user_count or 0),
            ),
            reverse=True,
        )
        current = ordered[0]
        previous = ordered[1] if len(ordered) > 1 else None
        current_score = score_signal_aggregate_v1(current)
        previous_score = score_signal_aggregate_v1(previous) if previous is not None else 0.0
        delta = round(current_score - previous_score, 4)
        if delta >= min_delta:
            trend_type = "rising"
        elif delta <= -min_delta:
            trend_type = "falling"
        else:
            trend_type = "stable"
        trends.append(
            AudienceTrendView(
                signal_key=signal_key,
                signal_type=str(current.signal_type or ""),
                target_name=str(current.target_name or ""),
                window_type=str(current.window_type or window_type or "long"),
                current_level=str(current.signal_level or "noise"),
                previous_score=previous_score,
                current_score=current_score,
                delta=delta,
                scale_confidence=float(getattr(current, "scale_confidence", 0.0) or 0.0),
                estimation_method=str(getattr(current, "estimation_method", "") or ""),
                trend_type=trend_type,
            )
        )

    return sorted(
        trends,
        key=lambda item: (abs(item.delta), item.current_score, item.signal_key),
        reverse=True,
    )[: max(1, limit)]


def derive_action_effectiveness(
    session: Session,
    project_id: str,
    *,
    limit: int = 8,
    window_type: str = "",
) -> list[dict[str, object]]:
    """Read noncausal observations against each action's frozen input snapshot."""
    from forwin.audience.aggregation import aggregate_view
    from forwin.audience.effects import observe_action_signal_change

    records = session.scalars(
        select(FeedbackActionRecord)
        .where(FeedbackActionRecord.project_id == project_id)
        .order_by(FeedbackActionRecord.created_at.desc())
        .limit(max(1, limit))
    ).all()
    signal_keys = {record.signal_key for record in records if record.signal_key}
    query = select(SignalWindowAggregate).where(
        SignalWindowAggregate.project_id == project_id,
        SignalWindowAggregate.signal_key.in_(signal_keys),
    )
    if window_type:
        query = query.where(SignalWindowAggregate.window_type == window_type)
    views = [aggregate_view(row) for row in session.scalars(query).all()] if signal_keys else []
    return [observe_action_signal_change(record, views) for record in records]


def _comment_scope_filters(
    session: Session,
    *,
    project_id: str,
    chapter_start: int,
    chapter_end: int,
) -> list[object]:
    del session
    return [
        PublisherRawComment.project_id == project_id,
        PublisherRawComment.source_status.in_(("chapter_known", "confirmed")),
        PublisherRawComment.source_chapter_number >= chapter_start,
        PublisherRawComment.source_chapter_number <= chapter_end,
    ]


# ── Reader-scale estimation ──────────────────────────────────────────

def _json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _iter_metric_candidates(payload: object):
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = _PLATFORM_METRIC_FIELDS.get(str(key))
            if normalized is not None:
                metric_name, confidence = normalized
                try:
                    number = int(float(str(value).replace(",", "")))
                except (TypeError, ValueError):
                    number = 0
                if number > 0:
                    yield metric_name, number, confidence
            yield from _iter_metric_candidates(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_metric_candidates(item)


def _resolve_platform_reader_scale(
    session: Session,
    project_id: str,
    *,
    chapter_start: int,
    chapter_end: int,
) -> ReaderScaleEstimate | None:
    scope_filters = _comment_scope_filters(
        session,
        project_id=project_id,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
    )
    comments = session.execute(
        select(PublisherRawComment.platform_id, PublisherRawComment.raw_payload_json)
        .where(*scope_filters)
        .order_by(PublisherRawComment.synced_at.desc())
        .limit(300)
    ).all()
    best: tuple[float, int, str, str] | None = None
    for platform_id, raw_payload_json in comments:
        for metric_name, number, confidence in _iter_metric_candidates(_json_object(raw_payload_json)):
            candidate = (confidence, number, str(platform_id or ""), metric_name)
            if best is None or candidate > best:
                best = candidate

    jobs = session.execute(
        select(PublisherCommentSyncJob.platform_id, PublisherCommentSyncJob.result_summary_json)
        .where(PublisherCommentSyncJob.project_id == project_id)
        .order_by(PublisherCommentSyncJob.updated_at.desc())
        .limit(20)
    ).all()
    for platform_id, result_summary_json in jobs:
        for metric_name, number, confidence in _iter_metric_candidates(_json_object(result_summary_json)):
            candidate = (confidence, number, str(platform_id or ""), metric_name)
            if best is None or candidate > best:
                best = candidate

    if best is None:
        return None
    confidence, number, platform_id, metric_name = best
    return ReaderScaleEstimate(
        reader_estimate=max(1, int(number)),
        estimation_method=f"platform_metric:{platform_id or 'unknown'}:{metric_name}",
        confidence=round(float(confidence), 3),
    )


def _comment_proxy_reader_scale(
    session: Session,
    project_id: str,
    *,
    chapter_start: int,
    chapter_end: int,
    comment_to_reader_ratio: int,
) -> ReaderScaleEstimate:
    scope_filters = _comment_scope_filters(
        session,
        project_id=project_id,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
    )
    total_comments = session.execute(
        select(func.count(func.distinct(PublisherRawComment.id))).where(*scope_filters)
    ).scalar_one()
    return ReaderScaleEstimate(
        reader_estimate=int(total_comments) * max(1, comment_to_reader_ratio),
        estimation_method="comment_proxy",
        confidence=0.35,
    )


def estimate_reader_scale(
    session: Session,
    project_id: str,
    *,
    chapter_number: int,
    comment_to_reader_ratio: int = 80,
) -> ReaderScaleSnapshot:
    """Estimate reader count, preferring platform metrics over comment proxy."""
    existing = session.execute(
        select(ReaderScaleSnapshot)
        .where(
            ReaderScaleSnapshot.project_id == project_id,
            ReaderScaleSnapshot.chapter_number == chapter_number,
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None and str(existing.estimation_method or "").startswith("platform_metric:"):
        return existing

    window_start = max(1, chapter_number - 20 + 1)
    estimate = _resolve_platform_reader_scale(
        session,
        project_id,
        chapter_start=window_start,
        chapter_end=chapter_number,
    ) or _comment_proxy_reader_scale(
        session,
        project_id,
        chapter_start=window_start,
        chapter_end=chapter_number,
        comment_to_reader_ratio=comment_to_reader_ratio,
    )
    if existing is not None:
        existing.reader_estimate = estimate.reader_estimate
        existing.estimation_method = estimate.estimation_method
        existing.tier = reader_tier_for_count(estimate.reader_estimate)
        session.add(existing)
        session.flush()
        return existing
    reader_estimate = estimate.reader_estimate
    tier = reader_tier_for_count(reader_estimate)

    snapshot = ReaderScaleSnapshot(
        id=new_id(),
        project_id=project_id,
        chapter_number=chapter_number,
        reader_estimate=reader_estimate,
        estimation_method=estimate.estimation_method,
        tier=tier,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


# ── Signal Aggregator ────────────────────────────────────────────────

class SignalAggregator:
    """Adapt the shared computation owner to the existing named windows."""

    def aggregate(
        self, session, project_id, chapter_number, *, comment_to_reader_ratio=80
    ):
        from forwin.audience.aggregation import aggregate_window

        scale = estimate_reader_scale(
            session,
            project_id,
            chapter_number=chapter_number,
            comment_to_reader_ratio=comment_to_reader_ratio,
        )
        return [
            row
            for window in WINDOWS
            for row in aggregate_window(
                session,
                project_id=project_id,
                chapter_start=max(1, chapter_number - window.size + 1),
                chapter_end=chapter_number,
                window_type=window.name,
                scale=scale,
            )
        ]


# ── Feedback Cooldown ────────────────────────────────────────────────

class FeedbackCooldown:
    """Only explicit selection starts a per-signal repetition cooldown."""

    def __init__(self, cooldown_chapters: int = 3) -> None:
        self.cooldown_chapters = max(1, cooldown_chapters)

    def is_cooled(
        self,
        session: Session,
        project_id: str,
        signal_key: str,
        chapter_number: int,
    ) -> bool:
        """Return True if the signal is past its cooldown and can trigger an action."""
        last_action = session.execute(
            select(FeedbackActionRecord)
            .where(
                FeedbackActionRecord.project_id == project_id,
                FeedbackActionRecord.signal_key == signal_key,
                FeedbackActionRecord.status == "selected",
                FeedbackActionRecord.source_qualified.is_(True),
            )
            .order_by(FeedbackActionRecord.cooldown_until_chapter.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last_action is None:
            return True
        return chapter_number >= last_action.cooldown_until_chapter

    def _latest_actions_by_signal_key(
        self,
        session: Session,
        *,
        project_id: str,
        signal_keys: Sequence[str],
    ) -> dict[str, FeedbackActionRecord]:
        normalized_signal_keys = [
            str(signal_key or "").strip()
            for signal_key in signal_keys
            if str(signal_key or "").strip()
        ]
        if not normalized_signal_keys:
            return {}
        ranked = (
            select(
                FeedbackActionRecord.id.label("row_id"),
                FeedbackActionRecord.signal_key.label("signal_key"),
                func.row_number()
                .over(
                    partition_by=FeedbackActionRecord.signal_key,
                    order_by=(
                        FeedbackActionRecord.cooldown_until_chapter.desc(),
                        FeedbackActionRecord.id.desc(),
                    ),
                )
                .label("rn"),
            )
            .where(
                FeedbackActionRecord.project_id == project_id,
                FeedbackActionRecord.signal_key.in_(normalized_signal_keys),
                FeedbackActionRecord.status == "selected",
                FeedbackActionRecord.source_qualified.is_(True),
            )
            .subquery()
        )
        rows = session.execute(
            select(FeedbackActionRecord)
            .join(ranked, FeedbackActionRecord.id == ranked.c.row_id)
            .where(ranked.c.rn == 1)
        ).scalars().all()
        return {
            str(row.signal_key or "").strip(): row
            for row in rows
            if str(row.signal_key or "").strip()
        }

    def filter_actionable(
        self,
        session: Session,
        project_id: str,
        chapter_number: int,
        aggregates: Sequence[SignalWindowAggregate],
    ) -> list[SignalWindowAggregate]:
        """Return only aggregates that are confirmed+ and past cooldown."""
        _ACTIONABLE_LEVELS = frozenset({"confirmed", "watchlist"})
        latest_actions = self._latest_actions_by_signal_key(
            session,
            project_id=project_id,
            signal_keys=[
                str(agg.signal_key or "").strip()
                for agg in aggregates
                if agg.signal_level in _ACTIONABLE_LEVELS
            ],
        )
        result: list[SignalWindowAggregate] = []
        for agg in aggregates:
            if agg.signal_level not in _ACTIONABLE_LEVELS:
                continue
            last_action = latest_actions.get(str(agg.signal_key or "").strip())
            if last_action is not None and chapter_number < last_action.cooldown_until_chapter:
                continue
            result.append(agg)
        return result


# ── Convenience: run full Phase B+C pass ─────────────────────────────

@dataclass(slots=True)
class FeedbackPassResult:
    """Result of a full feedback aggregation + action mapping pass."""
    all_aggregates: list[SignalWindowAggregate]
    actionable: list[SignalWindowAggregate]
    hint_pack: object  # AudienceHintPack from feedback_actions


def run_feedback_aggregation_pass(
    session: Session,
    project_id: str,
    chapter_number: int,
    *,
    cooldown_chapters: int = 3,
    comment_to_reader_ratio: int = 80,
) -> FeedbackPassResult:
    """Run the full Phase B+C pipeline: aggregate → cooldown → actions → hints.

    Returns a FeedbackPassResult with aggregates, actionable signals, and hint pack.
    Called from the pipeline loop after each accepted chapter.
    """
    from forwin.audience.actions import build_audience_hint_pack_from_aggregates
    from forwin.canon.projection_lock import lock_projection_project

    # Serialize snapshot FK inserts and selection before acquiring child locks.
    # Comment analysis, which calls the model, has already committed separately.
    lock_projection_project(session, project_id)

    aggregator = SignalAggregator()
    all_aggregates = aggregator.aggregate(
        session,
        project_id,
        chapter_number,
        comment_to_reader_ratio=comment_to_reader_ratio,
    )

    cooldown = FeedbackCooldown(cooldown_chapters=cooldown_chapters)
    actionable = cooldown.filter_actionable(
        session, project_id, chapter_number, all_aggregates
    )

    if actionable:
        logger.info(
            "Feedback pass: %d/%d signals actionable at chapter %d for project %s",
            len(actionable), len(all_aggregates), chapter_number, project_id,
        )
        for agg in actionable:
            logger.info(
                "  → %s [%s] severity=%d users=%d tier=%d",
                agg.signal_key, agg.signal_level, agg.max_severity,
                agg.unique_user_count, agg.reader_tier,
            )

    # Phase C: map to actions and build hint pack
    hint_pack = build_audience_hint_pack_from_aggregates(
        session,
        project_id,
        chapter_number,
        # Preserve opposite-direction evidence even while its action is cooling.
        # Selection applies cooldown after the unique mapper sees the full window.
        actionable=all_aggregates,
        cooldown=cooldown,
    )

    return FeedbackPassResult(
        all_aggregates=all_aggregates,
        actionable=actionable,
        hint_pack=hint_pack,
    )
