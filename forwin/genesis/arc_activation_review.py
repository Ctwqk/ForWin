from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.models.draft import ChapterDraft
from forwin.models.audit import DecisionEvent
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ChapterPlan
from forwin.models.publisher import SignalWindowAggregate


@dataclass(frozen=True)
class ArcActivationReviewPack:
    project_id: str
    arc_number: int
    chapter_start: int
    accepted_chapter_summaries: list[dict[str, Any]] = field(default_factory=list)
    important_character_state: list[dict[str, Any]] = field(default_factory=list)
    book_state_facts: list[dict[str, Any]] = field(default_factory=list)
    open_obligations: list[dict[str, Any]] = field(default_factory=list)
    recent_director_imbalance: list[dict[str, Any]] = field(default_factory=list)
    audience_signals: list[dict[str, Any]] = field(default_factory=list)
    map_snapshot: dict[str, Any] = field(default_factory=dict)
    faction_snapshot: list[dict[str, Any]] = field(default_factory=list)

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "arc_number": self.arc_number,
            "chapter_start": self.chapter_start,
            "accepted_chapter_summaries": list(self.accepted_chapter_summaries),
            "important_character_state": list(self.important_character_state),
            "book_state_facts": list(self.book_state_facts),
            "open_obligations": list(self.open_obligations),
            "recent_director_imbalance": list(self.recent_director_imbalance),
            "audience_signals": list(self.audience_signals),
            "map_snapshot": dict(self.map_snapshot),
            "faction_snapshot": list(self.faction_snapshot),
        }


def build_arc_activation_review_pack(
    session: Session,
    *,
    project_id: str,
    arc_number: int,
    chapter_start: int,
    summary_limit: int = 8,
    node_limit: int = 8,
    fact_limit: int = 12,
) -> ArcActivationReviewPack:
    base_chapter = max(0, int(chapter_start or 0) - 1)
    repo = BookStateRepository(session)
    nodes = repo.list_world_nodes(project_id, as_of_chapter=base_chapter)
    facts = repo.list_fact_nodes(project_id, as_of_chapter=base_chapter)
    important_characters = [
        {
            "id": node.id,
            "name": node.name,
            "summary": node.summary,
            "importance": node.importance,
            "state": dict(node.state),
        }
        for node in sorted(
            [node for node in nodes if str(node.node_type) == "character"],
            key=lambda item: (int(item.importance or 0), item.name),
            reverse=True,
        )[:node_limit]
    ]
    faction_snapshot = [
        {
            "id": node.id,
            "name": node.name,
            "summary": node.summary,
            "state": dict(node.state),
        }
        for node in nodes
        if str(node.node_type) in {"faction", "organization", "group"}
    ][:node_limit]
    return ArcActivationReviewPack(
        project_id=project_id,
        arc_number=int(arc_number or 0),
        chapter_start=int(chapter_start or 0),
        accepted_chapter_summaries=_accepted_chapter_summaries(
            session,
            project_id=project_id,
            before_chapter=chapter_start,
            limit=summary_limit,
        ),
        important_character_state=important_characters,
        book_state_facts=[
            {
                "id": fact.id,
                "proposition": fact.proposition,
                "fact_type": fact.fact_type,
                "source_refs": list(fact.source_refs),
            }
            for fact in sorted(
                facts,
                key=lambda item: (int(item.created_at_chapter or 0), item.id),
                reverse=True,
            )[:fact_limit]
        ],
        open_obligations=_open_obligations(session, project_id=project_id, limit=8),
        recent_director_imbalance=_recent_director_events(
            session,
            project_id=project_id,
            limit=6,
        ),
        audience_signals=_audience_signals(session, project_id=project_id, limit=6),
        faction_snapshot=faction_snapshot,
    )


def _accepted_chapter_summaries(
    session: Session,
    *,
    project_id: str,
    before_chapter: int,
    limit: int,
) -> list[dict[str, Any]]:
    plans = list(
        session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.status == "accepted",
                ChapterPlan.chapter_number < int(before_chapter or 0),
            )
            .order_by(ChapterPlan.chapter_number.desc())
            .limit(limit)
        ).scalars()
    )
    rows: list[dict[str, Any]] = []
    for plan in reversed(plans):
        draft = session.execute(
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc(), ChapterDraft.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        summary = str(getattr(draft, "summary", "") or plan.one_line or "").strip()
        rows.append(
            {
                "chapter_number": int(plan.chapter_number or 0),
                "title": str(plan.title or ""),
                "summary": summary,
            }
        )
    return rows


def _open_obligations(
    session: Session,
    *,
    project_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = list(
        session.execute(
            select(NarrativeObligationRow)
            .where(
                NarrativeObligationRow.project_id == project_id,
                NarrativeObligationRow.status.in_(("active", "planned", "proposed")),
            )
            .order_by(
                NarrativeObligationRow.deadline_chapter.asc(),
                NarrativeObligationRow.priority.asc(),
            )
            .limit(limit)
        ).scalars()
    )
    return [
        {
            "id": row.id,
            "priority": row.priority,
            "status": row.status,
            "obligation_type": row.obligation_type,
            "summary": row.summary,
            "deadline_chapter": int(row.deadline_chapter or 0),
            "payoff_test": row.payoff_test,
        }
        for row in rows
    ]


def _recent_director_events(
    session: Session,
    *,
    project_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = list(
        session.execute(
            select(DecisionEvent)
            .where(
                DecisionEvent.project_id == project_id,
                DecisionEvent.event_family.in_(
                    ("director_imbalance", "evaluation_verdict")
                ),
            )
            .order_by(DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
            .limit(limit)
        ).scalars()
    )
    return [
        {
            "id": row.id,
            "event_type": row.event_type,
            "summary": row.summary,
            "chapter_number": int(row.chapter_number or 0),
            "payload": _json_load(row.payload_json),
        }
        for row in rows
    ]


def _audience_signals(
    session: Session,
    *,
    project_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = list(
        session.execute(
            select(SignalWindowAggregate)
            .where(
                SignalWindowAggregate.project_id == project_id,
                SignalWindowAggregate.signal_level.in_(
                    ("confirmed", "watchlist", "candidate")
                ),
            )
            .order_by(
                SignalWindowAggregate.window_chapter_end.desc(),
                SignalWindowAggregate.max_severity.desc(),
                SignalWindowAggregate.unique_user_count.desc(),
            )
            .limit(limit)
        ).scalars()
    )
    return [
        {
            "signal_key": row.signal_key,
            "signal_type": row.signal_type,
            "target_name": row.target_name,
            "signal_level": row.signal_level,
            "window_chapter_start": int(row.window_chapter_start or 0),
            "window_chapter_end": int(row.window_chapter_end or 0),
        }
        for row in rows
    ]


def _json_load(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = ["ArcActivationReviewPack", "build_arc_activation_review_pack"]
