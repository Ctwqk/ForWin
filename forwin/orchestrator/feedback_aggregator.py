"""Phase B of the Audience Feedback Layer."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import logging
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from forwin.models import (
    CommentSignalCandidate,
    FeedbackActionRecord,
    PublisherRawComment,
    ReaderScaleSnapshot,
    SignalWindowAggregate,
    new_id,
)
from forwin.orchestrator.comment_analysis import classify_signal_level

logger = logging.getLogger(__name__)

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


# ── Reader-scale estimation ──────────────────────────────────────────

def estimate_reader_scale(
    session: Session,
    project_id: str,
    *,
    chapter_number: int,
    comment_to_reader_ratio: int = 80,
) -> ReaderScaleSnapshot:
    """Estimate reader count from recent comment volume and persist a snapshot."""
    existing = session.execute(
        select(ReaderScaleSnapshot)
        .where(
            ReaderScaleSnapshot.project_id == project_id,
            ReaderScaleSnapshot.chapter_number == chapter_number,
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    window_start = max(1, chapter_number - 20 + 1)
    total_comments = session.execute(
        select(func.count(func.distinct(CommentSignalCandidate.source_comment_id)))
        .where(
            CommentSignalCandidate.project_id == project_id,
            CommentSignalCandidate.chapter_number >= window_start,
            CommentSignalCandidate.chapter_number <= chapter_number,
        )
    ).scalar_one()
    reader_estimate = int(total_comments) * max(1, comment_to_reader_ratio)
    tier = reader_tier_for_count(reader_estimate)

    snapshot = ReaderScaleSnapshot(
        id=new_id(),
        project_id=project_id,
        chapter_number=chapter_number,
        reader_estimate=reader_estimate,
        estimation_method="comment_proxy",
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
                    max_severity=bucket.max_severity,
                    avg_confidence=round(avg_conf, 3),
                    signal_level=level,
                )
                session.add(row)
                results.append(row)

                # Also update the per-signal level on the candidates
                _update_candidate_levels(session, project_id, key, level)

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
    project_id: str,
    signal_key: str,
    level: str,
) -> None:
    """Propagate the aggregated level back to individual candidates."""
    parts = signal_key.split(":", 2)
    if len(parts) < 2:
        return
    signal_type = parts[0]
    target_name = parts[2] if len(parts) > 2 else ""

    stmt = (
        select(CommentSignalCandidate)
        .where(
            CommentSignalCandidate.project_id == project_id,
            CommentSignalCandidate.signal_type == signal_type,
        )
    )
    if target_name and target_name not in {"general", "整体"}:
        stmt = stmt.where(CommentSignalCandidate.target_name == target_name)
    elif target_name == "整体":
        stmt = stmt.where(
            or_(
                CommentSignalCandidate.target_name == "",
                CommentSignalCandidate.target_name == "整体",
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
