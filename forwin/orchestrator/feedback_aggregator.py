"""Phase B of the Audience Feedback Layer."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import logging
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from forwin.models import (
    ChapterPlan,
    CommentSignalCandidate,
    FeedbackActionRecord,
    Project,
    PublisherCommentSyncJob,
    PublisherRawComment,
    ReaderScaleSnapshot,
    SignalWindowAggregate,
    new_id,
)
from forwin.orchestrator.comment_analysis import classify_signal_level
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
    window_type: str = "long",
) -> list[dict[str, object]]:
    """Compare post-action signal score to the latest pre-action score."""
    records = session.execute(
        select(FeedbackActionRecord)
        .where(FeedbackActionRecord.project_id == project_id)
        .order_by(FeedbackActionRecord.created_at.desc())
        .limit(max(1, limit))
    ).scalars().all()
    results: list[dict[str, object]] = []
    for record in records:
        rows = session.execute(
            select(SignalWindowAggregate)
            .where(
                SignalWindowAggregate.project_id == project_id,
                SignalWindowAggregate.signal_key == record.signal_key,
                SignalWindowAggregate.window_type == window_type,
            )
            .order_by(SignalWindowAggregate.window_chapter_end.asc(), SignalWindowAggregate.created_at.asc())
        ).scalars().all()
        before_rows = [
            row
            for row in rows
            if int(row.window_chapter_end or 0) <= int(record.triggered_at_chapter or 0)
        ]
        after_rows = [
            row
            for row in rows
            if int(row.window_chapter_end or 0) > int(record.triggered_at_chapter or 0)
        ]
        before = before_rows[-1] if before_rows else None
        after = after_rows[-1] if after_rows else None
        before_score = score_signal_aggregate_v1(before) if before is not None else 0.0
        after_score = score_signal_aggregate_v1(after) if after is not None else 0.0
        if before is None or after is None:
            outcome = "insufficient_data"
        else:
            delta = round(after_score - before_score, 4)
            if delta <= -0.05:
                outcome = "improved"
            elif delta >= 0.05:
                outcome = "worsened"
            else:
                outcome = "unchanged"
        results.append(
            {
                "signal_key": str(record.signal_key or ""),
                "signal_type": str(record.signal_type or ""),
                "action_type": str(record.action_type or ""),
                "triggered_at_chapter": int(record.triggered_at_chapter or 0),
                "cooldown_until_chapter": int(record.cooldown_until_chapter or 0),
                "before_score": before_score,
                "after_score": after_score,
                "outcome": outcome,
                "notes": str(record.notes or ""),
            }
        )
    return results


def _comment_scope_filters(
    session: Session,
    *,
    project_id: str,
    chapter_start: int,
    chapter_end: int,
) -> list[object]:
    chapter_titles = [
        str(item).strip()
        for item in session.execute(
            select(ChapterPlan.title).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number >= chapter_start,
                ChapterPlan.chapter_number <= chapter_end,
            )
        ).scalars().all()
        if str(item).strip()
    ]
    has_project_scoped_comments = bool(
        session.execute(
            select(func.count(PublisherRawComment.id)).where(
                PublisherRawComment.project_id == project_id
            )
        ).scalar_one()
    )
    filters: list[object] = []
    if has_project_scoped_comments:
        filters.append(PublisherRawComment.project_id == project_id)
    else:
        project_title = session.execute(
            select(Project.title).where(Project.id == project_id).limit(1)
        ).scalar_one_or_none()
        normalized_title = str(project_title or "").strip()
        if normalized_title:
            filters.append(PublisherRawComment.work_name == normalized_title)
    if chapter_titles:
        filters.append(
            or_(
                PublisherRawComment.chapter_title.in_(chapter_titles),
                PublisherRawComment.chapter_title == "",
            )
        )
    return filters


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
    """Window-based aggregation of CommentSignalCandidate rows.

    For each window × signal_key, computes M / U / C / R and applies the
    shared hard-rule classifier. Persists results as SignalWindowAggregate rows.
    """

    def aggregate(
        self,
        session: Session,
        project_id: str,
        chapter_number: int,
        *,
        comment_to_reader_ratio: int = 80,
    ) -> list[SignalWindowAggregate]:
        """Run windowed aggregation and return all aggregate rows created."""
        scale = estimate_reader_scale(
            session,
            project_id,
            chapter_number=chapter_number,
            comment_to_reader_ratio=comment_to_reader_ratio,
        )

        results: list[SignalWindowAggregate] = []
        candidate_levels: dict[tuple[str, str, str], str] = {}
        for window in WINDOWS:
            window_start = max(1, chapter_number - window.size + 1)
            window_end = chapter_number

            # Load signals in window
            signals = session.execute(
                select(CommentSignalCandidate)
                .where(
                    CommentSignalCandidate.project_id == project_id,
                    CommentSignalCandidate.chapter_number >= window_start,
                    CommentSignalCandidate.chapter_number <= window_end,
                )
            ).scalars().all()

            comment_map = {
                row.id: row
                for row in session.execute(
                    select(PublisherRawComment).where(
                        PublisherRawComment.id.in_(
                            {signal.source_comment_id for signal in signals}
                        )
                    )
                ).scalars().all()
            }

            # Count total comments in window (C)
            total_comments = session.execute(
                select(func.count(func.distinct(CommentSignalCandidate.source_comment_id)))
                .where(
                    CommentSignalCandidate.project_id == project_id,
                    CommentSignalCandidate.chapter_number >= window_start,
                    CommentSignalCandidate.chapter_number <= window_end,
                )
            ).scalar_one()

            # Group by signal_key
            buckets: dict[str, _Bucket] = defaultdict(_Bucket)
            for sig in signals:
                key = f"{sig.signal_type}:{sig.target_type}:{sig.target_name or 'general'}"
                bucket = buckets[key]
                bucket.signal_type = sig.signal_type
                bucket.target_type = sig.target_type
                bucket.target_name = sig.target_name
                source_comment = comment_map.get(sig.source_comment_id)
                user_key = ""
                if source_comment is not None:
                    user_key = (
                        str(source_comment.author_id or "").strip()
                        or str(source_comment.author_name or "").strip()
                    )
                bucket.user_keys.add(user_key or sig.source_comment_id)
                bucket.source_comment_ids.add(sig.source_comment_id)
                bucket.hit_count += 1
                bucket.max_severity = max(bucket.max_severity, sig.severity)
                bucket.confidence_sum += sig.confidence
                if sig.chapter_number > 0:
                    bucket.chapters.add(sig.chapter_number)

            # Delete old aggregates for this window
            session.execute(
                SignalWindowAggregate.__table__.delete().where(
                    SignalWindowAggregate.project_id == project_id,
                    SignalWindowAggregate.window_type == window.name,
                    SignalWindowAggregate.window_chapter_end == window_end,
                )
            )

            for key, bucket in buckets.items():
                unique_users = len(bucket.user_keys)
                spans_chapters = len(bucket.chapters)
                avg_conf = bucket.confidence_sum / max(1, bucket.hit_count)
                level = classify_signal_level(
                    unique_users=unique_users,
                    spans_chapters=spans_chapters,
                    severity=bucket.max_severity,
                    signal_type=bucket.signal_type,
                )

                row = SignalWindowAggregate(
                    id=new_id(),
                    project_id=project_id,
                    signal_key=key,
                    signal_type=bucket.signal_type,
                    target_type=bucket.target_type,
                    target_name=bucket.target_name,
                    window_type=window.name,
                    window_chapter_start=window_start,
                    window_chapter_end=window_end,
                    hit_comment_count=bucket.hit_count,
                    unique_user_count=unique_users,
                    total_comment_count=total_comments,
                    reader_estimate=scale.reader_estimate,
                    reader_tier=scale.tier,
                    estimation_method=scale.estimation_method,
                    scale_confidence=(
                        0.9
                        if str(scale.estimation_method or "").startswith("platform_metric:")
                        else 0.35
                    ),
                    max_severity=bucket.max_severity,
                    avg_confidence=round(avg_conf, 3),
                    signal_level=level,
                )
                session.add(row)
                results.append(row)
                candidate_key = (
                    str(bucket.signal_type or "").strip(),
                    str(bucket.target_type or "").strip(),
                    str(bucket.target_name or "").strip(),
                )
                previous_level = candidate_levels.get(candidate_key, "noise")
                if _SIGNAL_LEVEL_ORDER.get(level, 0) >= _SIGNAL_LEVEL_ORDER.get(previous_level, 0):
                    candidate_levels[candidate_key] = level

        for (signal_type, target_type, target_name), level in candidate_levels.items():
            _update_candidate_levels(
                session,
                project_id=project_id,
                signal_type=signal_type,
                target_type=target_type,
                target_name=target_name,
                level=level,
            )

        if results:
            session.flush()
        return results


class _Bucket:
    __slots__ = (
        "signal_type", "target_type", "target_name",
        "user_keys", "source_comment_ids", "hit_count", "max_severity",
        "confidence_sum", "chapters",
    )

    def __init__(self) -> None:
        self.signal_type = ""
        self.target_type = ""
        self.target_name = ""
        self.user_keys: set[str] = set()
        self.source_comment_ids: set[str] = set()
        self.hit_count = 0
        self.max_severity = 0
        self.confidence_sum = 0.0
        self.chapters: set[int] = set()


def _update_candidate_levels(
    session: Session,
    *,
    project_id: str,
    signal_type: str,
    target_type: str,
    target_name: str,
    level: str,
) -> None:
    """Propagate the aggregated level back to individual candidates."""
    normalized_target_name = str(target_name or "").strip()

    stmt = (
        select(CommentSignalCandidate)
        .where(
            CommentSignalCandidate.project_id == project_id,
            CommentSignalCandidate.signal_type == signal_type,
            CommentSignalCandidate.target_type == target_type,
        )
    )
    if normalized_target_name and normalized_target_name not in {"general", "整体"}:
        stmt = stmt.where(CommentSignalCandidate.target_name == normalized_target_name)
    else:
        stmt = stmt.where(
            or_(
                CommentSignalCandidate.target_name == "",
                CommentSignalCandidate.target_name == "整体",
                CommentSignalCandidate.target_name == "general",
            )
        )

    for candidate in session.execute(stmt).scalars().all():
        candidate.signal_level = level


# ── Feedback Cooldown ────────────────────────────────────────────────

class FeedbackCooldown:
    """Tracks which signals have been acted on and enforces per-signal cooldowns."""

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
            )
            .order_by(FeedbackActionRecord.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last_action is None:
            return True
        return chapter_number >= last_action.cooldown_until_chapter

    def record_action(
        self,
        session: Session,
        *,
        project_id: str,
        signal_key: str,
        signal_type: str,
        action_type: str,
        chapter_number: int,
        notes: str = "",
    ) -> FeedbackActionRecord:
        """Record that an action was taken on a signal, starting cooldown."""
        record = FeedbackActionRecord(
            id=new_id(),
            project_id=project_id,
            signal_key=signal_key,
            signal_type=signal_type,
            action_type=action_type,
            triggered_at_chapter=chapter_number,
            cooldown_until_chapter=chapter_number + self.cooldown_chapters,
            notes=notes,
        )
        session.add(record)
        session.flush()
        return record

    def filter_actionable(
        self,
        session: Session,
        project_id: str,
        chapter_number: int,
        aggregates: Sequence[SignalWindowAggregate],
    ) -> list[SignalWindowAggregate]:
        """Return only aggregates that are confirmed+ and past cooldown."""
        _ACTIONABLE_LEVELS = frozenset({"confirmed", "watchlist"})
        result: list[SignalWindowAggregate] = []
        for agg in aggregates:
            if agg.signal_level not in _ACTIONABLE_LEVELS:
                continue
            if not self.is_cooled(session, project_id, agg.signal_key, chapter_number):
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
    Called from the orchestrator loop after each accepted chapter.
    """
    from forwin.orchestrator.feedback_actions import build_audience_hint_pack_from_aggregates

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
        actionable=actionable,
        cooldown=cooldown,
    )

    return FeedbackPassResult(
        all_aggregates=all_aggregates,
        actionable=actionable,
        hint_pack=hint_pack,
    )
