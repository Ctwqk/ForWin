from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.api_schemas import (
    ChapterInfo,
    EntityInfo,
    ProjectDetail,
    ProjectSummary,
    ProvisionalBandDetail,
    ProvisionalChapterLedgerInfo,
    ThreadInfo,
)
from forwin.models.draft import ChapterDraft
from forwin.models.entity import Entity
from forwin.models.phase import (
    ArcEnvelopeAnalysis,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalBandExecution,
    ProvisionalChapterLedger,
)
from forwin.models.phase4 import NPCIntentSnapshot, WorldSimulationTurn
from forwin.models.project import ChapterPlan, Project
from forwin.models.thread import PlotThread
from forwin.state.query_helpers import (
    load_latest_active_arc_envelope_by_project,
    load_latest_arc_envelope_analysis_by_project,
    load_latest_drafts_by_plan_id,
    load_latest_provisional_band_execution_by_project,
    load_latest_replan_event_by_project,
    load_latest_stage_analysis_by_project,
    load_latest_world_turn_by_project,
)


DisplayDatetime = Callable[[datetime | None], str]


def project_arc_snapshot_payload(
    latest_arc_envelope,
    latest_arc_analysis,
    latest_provisional,
) -> dict[str, Any]:
    payload = {
        "active_arc_policy_tier": "",
        "active_arc_target_size": 0,
        "active_arc_soft_min": 0,
        "active_arc_soft_max": 0,
        "active_arc_detailed_band_size": 0,
        "active_arc_frozen_zone_size": 0,
        "active_arc_confidence": 0.0,
        "active_arc_recommendation": "",
        "active_arc_analysis_confidence": 0.0,
        "active_arc_evidence": [],
        "active_arc_expansion_signals": [],
        "active_arc_compression_signals": [],
        "provisional_band_id": "",
        "provisional_aggregate_verdict": "",
        "provisional_preview_char_count": 0,
        "provisional_issue_count": 0,
        "provisional_failure_count": 0,
    }
    if latest_arc_envelope is not None:
        payload.update(
            {
                "active_arc_policy_tier": latest_arc_envelope.source_policy_tier,
                "active_arc_target_size": latest_arc_envelope.resolved_target_size,
                "active_arc_soft_min": latest_arc_envelope.resolved_soft_min,
                "active_arc_soft_max": latest_arc_envelope.resolved_soft_max,
                "active_arc_detailed_band_size": latest_arc_envelope.detailed_band_size,
                "active_arc_frozen_zone_size": latest_arc_envelope.frozen_zone_size,
                "active_arc_confidence": latest_arc_envelope.current_confidence,
            }
        )
    if latest_arc_analysis is not None:
        payload.update(
            {
                "active_arc_recommendation": latest_arc_analysis.recommendation,
                "active_arc_analysis_confidence": latest_arc_analysis.confidence,
                "active_arc_evidence": _json_list_strings(latest_arc_analysis.evidence_json),
                "active_arc_expansion_signals": _json_list_strings(
                    latest_arc_analysis.expansion_signals_json
                ),
                "active_arc_compression_signals": _json_list_strings(
                    latest_arc_analysis.compression_signals_json
                ),
            }
        )
    if latest_provisional is not None:
        payload.update(
            {
                "provisional_band_id": latest_provisional.band_id,
                "provisional_aggregate_verdict": latest_provisional.aggregate_verdict,
                "provisional_preview_char_count": latest_provisional.preview_char_count,
                "provisional_issue_count": latest_provisional.issue_count,
                "provisional_failure_count": latest_provisional.failure_count,
            }
        )
    return payload


def load_recent_replan_events_by_project(
    session: Session,
    project_ids: list[str],
    *,
    limit: int,
) -> dict[str, list[ProjectReplanEvent]]:
    if not project_ids:
        return {}
    rows = session.execute(
        select(ProjectReplanEvent)
        .where(ProjectReplanEvent.project_id.in_(project_ids))
        .order_by(
            ProjectReplanEvent.project_id.asc(),
            ProjectReplanEvent.trigger_chapter.desc(),
            ProjectReplanEvent.created_at.desc(),
        )
    ).scalars().all()
    grouped: dict[str, list[ProjectReplanEvent]] = defaultdict(list)
    for row in rows:
        items = grouped[row.project_id]
        if len(items) < limit:
            items.append(row)
    return dict(grouped)


def load_recent_npc_intents_by_project(
    session: Session,
    project_ids: list[str],
    *,
    limit: int,
) -> dict[str, list[NPCIntentSnapshot]]:
    if not project_ids:
        return {}
    rows = session.execute(
        select(NPCIntentSnapshot)
        .where(NPCIntentSnapshot.project_id.in_(project_ids))
        .order_by(
            NPCIntentSnapshot.project_id.asc(),
            NPCIntentSnapshot.chapter_number.desc(),
            NPCIntentSnapshot.urgency.desc(),
            NPCIntentSnapshot.created_at.desc(),
        )
    ).scalars().all()
    grouped: dict[str, list[NPCIntentSnapshot]] = defaultdict(list)
    for row in rows:
        items = grouped[row.project_id]
        if len(items) < limit:
            items.append(row)
    return dict(grouped)


def latest_draft_map(session: Session, chapter_plan_ids: list[str]) -> dict[str, ChapterDraft]:
    return load_latest_drafts_by_plan_id(session, chapter_plan_ids)


def load_project_runtime_maps(
    session: Session,
    project_ids: list[str],
) -> dict[str, dict[str, Any]]:
    latest_stage_map = load_latest_stage_analysis_by_project(session, project_ids)
    last_replan_map = load_latest_replan_event_by_project(session, project_ids)
    latest_world_map = load_latest_world_turn_by_project(session, project_ids)
    latest_arc_envelope_map = load_latest_active_arc_envelope_by_project(session, project_ids)
    latest_arc_analysis_map = load_latest_arc_envelope_analysis_by_project(session, project_ids)
    provisional_map = load_latest_provisional_band_execution_by_project(session, project_ids)
    recent_replans_map = load_recent_replan_events_by_project(session, project_ids, limit=5)
    recent_npc_map = load_recent_npc_intents_by_project(session, project_ids, limit=6)
    return {
        "latest_stage_map": latest_stage_map,
        "last_replan_map": last_replan_map,
        "latest_world_map": latest_world_map,
        "latest_arc_envelope_map": latest_arc_envelope_map,
        "latest_arc_analysis_map": latest_arc_analysis_map,
        "provisional_map": provisional_map,
        "recent_replans_map": recent_replans_map,
        "recent_npc_map": recent_npc_map,
    }


def build_project_summaries(
    *,
    session: Session,
    projects: list[Project],
    display_datetime: DisplayDatetime,
) -> list[ProjectSummary]:
    project_ids = [project.id for project in projects]
    plans = (
        session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id.in_(project_ids))
            .order_by(ChapterPlan.project_id, ChapterPlan.chapter_number)
        ).scalars().all()
        if project_ids
        else []
    )
    draft_map = latest_draft_map(session, [plan.id for plan in plans])
    runtime_maps = load_project_runtime_maps(session, project_ids)
    chapters_by_project: dict[str, list[dict[str, object]]] = {}
    for plan in plans:
        draft = draft_map.get(plan.id)
        chapters_by_project.setdefault(plan.project_id, []).append(
            {
                "chapter_number": plan.chapter_number,
                "title": plan.title,
                "status": plan.status,
                "char_count": draft.char_count if draft else 0,
                "summary": draft.summary if draft else "",
            }
        )

    payload: list[ProjectSummary] = []
    for project in projects:
        latest_stage = runtime_maps["latest_stage_map"].get(project.id)
        last_replan = runtime_maps["last_replan_map"].get(project.id)
        latest_world = runtime_maps["latest_world_map"].get(project.id)
        latest_arc_envelope = runtime_maps["latest_arc_envelope_map"].get(project.id)
        latest_arc_analysis = runtime_maps["latest_arc_analysis_map"].get(project.id)
        latest_provisional = runtime_maps["provisional_map"].get(project.id)
        payload.append(
            ProjectSummary(
                id=project.id,
                title=project.title,
                genre=project.genre,
                premise=project.premise[:100] + "..." if len(project.premise) > 100 else project.premise,
                created_at=display_datetime(project.created_at),
                latest_stage=latest_stage.stage_label if latest_stage else "",
                pacing_verdict=latest_stage.pacing_verdict if latest_stage else "",
                pacing_summary=latest_stage.pacing_summary if latest_stage else "",
                last_replan_status=last_replan.status if last_replan else "",
                last_replan_strategy=last_replan.strategy if last_replan else "",
                last_replan_reason=last_replan.reason if last_replan else "",
                current_time_label=latest_stage.timeline_label if latest_stage else "",
                world_pressure_level=latest_world.pressure_level if latest_world else "",
                world_pressure_summary=latest_world.pressure_summary if latest_world else "",
                chapters=chapters_by_project.get(project.id, []),
                **project_arc_snapshot_payload(
                    latest_arc_envelope,
                    latest_arc_analysis,
                    latest_provisional,
                ),
            )
        )
    return payload


def build_project_detail(
    *,
    session: Session,
    project: Project,
    display_datetime: DisplayDatetime,
) -> ProjectDetail:
    project_id = project.id
    entities = session.execute(
        select(Entity).where(Entity.project_id == project_id, Entity.is_active == True)
    ).scalars().all()
    characters = [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "character"
    ]
    locations = [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "location"
    ]
    factions = [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "faction"
    ]

    threads = session.execute(
        select(PlotThread).where(PlotThread.project_id == project_id)
    ).scalars().all()
    thread_infos = [
        ThreadInfo(id=t.id, name=t.name, description=t.description, status=t.status, priority=t.priority)
        for t in threads
    ]

    plans = session.execute(
        select(ChapterPlan).where(ChapterPlan.project_id == project_id).order_by(ChapterPlan.chapter_number)
    ).scalars().all()
    draft_map = latest_draft_map(session, [plan.id for plan in plans])
    chapter_infos = [
        ChapterInfo(
            chapter_number=plan.chapter_number,
            title=plan.title,
            status=plan.status,
            char_count=draft_map.get(plan.id).char_count if draft_map.get(plan.id) else 0,
            summary=draft_map.get(plan.id).summary if draft_map.get(plan.id) else "",
        )
        for plan in plans
    ]

    runtime_maps = load_project_runtime_maps(session, [project_id])
    latest_stage = runtime_maps["latest_stage_map"].get(project_id)
    latest_world = runtime_maps["latest_world_map"].get(project_id)
    latest_arc_envelope = runtime_maps["latest_arc_envelope_map"].get(project_id)
    latest_arc_analysis = runtime_maps["latest_arc_analysis_map"].get(project_id)
    latest_provisional = runtime_maps["provisional_map"].get(project_id)
    npc_intents = runtime_maps["recent_npc_map"].get(project_id, [])
    replan_events = runtime_maps["recent_replans_map"].get(project_id, [])

    return ProjectDetail(
        id=project.id,
        title=project.title,
        premise=project.premise,
        genre=project.genre,
        setting_summary=project.setting_summary,
        characters=characters,
        locations=locations,
        factions=factions,
        threads=thread_infos,
        chapters=chapter_infos,
        latest_stage=latest_stage.stage_label if latest_stage else "",
        progress_ratio=latest_stage.progress_ratio if latest_stage else 0.0,
        pacing_verdict=latest_stage.pacing_verdict if latest_stage else "",
        pacing_summary=latest_stage.pacing_summary if latest_stage else "",
        current_time_label=latest_stage.timeline_label if latest_stage else "",
        world_pressure_level=latest_world.pressure_level if latest_world else "",
        world_pressure_summary=latest_world.pressure_summary if latest_world else "",
        npc_intent_count=len(npc_intents),
        **project_arc_snapshot_payload(
            latest_arc_envelope,
            latest_arc_analysis,
            latest_provisional,
        ),
        recent_npc_intents=[
            {
                "chapter_number": item.chapter_number,
                "entity_name": item.entity_name,
                "intent_kind": item.intent_kind,
                "objective": item.objective,
                "tactic": item.tactic,
                "urgency": item.urgency,
                "notes": item.notes,
            }
            for item in npc_intents
        ],
        recent_replans=[
            {
                "trigger_chapter": item.trigger_chapter,
                "risk_level": item.risk_level,
                "strategy": item.strategy,
                "status": item.status,
                "reason": item.reason,
                "cooldown_until_chapter": item.cooldown_until_chapter,
                "created_at": display_datetime(item.created_at),
            }
            for item in replan_events
        ],
    )


def latest_provisional_band_execution(
    session: Session,
    project_id: str,
    *,
    arc_id: str | None = None,
) -> ProvisionalBandExecution | None:
    stmt = select(ProvisionalBandExecution).where(
        ProvisionalBandExecution.project_id == project_id
    )
    if arc_id:
        stmt = stmt.where(ProvisionalBandExecution.arc_id == arc_id)
    return session.execute(
        stmt.order_by(ProvisionalBandExecution.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def provisional_chapter_ledgers(
    session: Session,
    *,
    project_id: str,
    arc_id: str,
    band_id: str,
) -> list[ProvisionalChapterLedger]:
    return list(
        session.execute(
            select(ProvisionalChapterLedger)
            .where(
                ProvisionalChapterLedger.project_id == project_id,
                ProvisionalChapterLedger.arc_id == arc_id,
                ProvisionalChapterLedger.band_id == band_id,
            )
            .order_by(
                ProvisionalChapterLedger.chapter_number.asc(),
                ProvisionalChapterLedger.created_at.asc(),
            )
        ).scalars().all()
    )


def build_provisional_band_detail(
    *,
    session: Session,
    project_id: str,
    latest: ProvisionalBandExecution,
    display_datetime: DisplayDatetime,
) -> ProvisionalBandDetail:
    ledgers = provisional_chapter_ledgers(
        session,
        project_id=project_id,
        arc_id=latest.arc_id,
        band_id=latest.band_id,
    )
    chapter_numbers = []
    try:
        chapter_numbers = json.loads(latest.chapter_numbers_json or "[]") or []
    except (json.JSONDecodeError, TypeError):
        chapter_numbers = []

    return ProvisionalBandDetail(
        project_id=project_id,
        arc_id=latest.arc_id,
        band_id=latest.band_id,
        aggregate_verdict=latest.aggregate_verdict,
        preview_char_count=latest.preview_char_count,
        issue_count=latest.issue_count,
        failure_count=latest.failure_count,
        artifact_path=latest.artifact_path,
        chapter_numbers=[int(item) for item in chapter_numbers if isinstance(item, int)],
        created_at=display_datetime(latest.created_at),
        chapters=[
            ProvisionalChapterLedgerInfo(
                chapter_number=row.chapter_number,
                title=row.title,
                summary=row.summary,
                verdict=row.verdict,
                char_count=row.char_count,
                artifact_meta_path=row.artifact_meta_path,
                draft_blob_path=row.draft_blob_path,
                current_time_label=row.current_time_label,
                projected_time_label=row.projected_time_label,
                state_changes=_load_json_list(row.state_changes_json),
                events=_load_json_list(row.events_json),
                thread_beats=_load_json_list(row.thread_beats_json),
                time_advance=_load_json_dict(row.time_advance_json),
                issues=_load_json_list(row.issues_json),
                error=row.error_text,
                created_at=display_datetime(row.created_at),
            )
            for row in ledgers
        ],
    )


def _json_list_strings(raw: str) -> list[str]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in payload if item is not None]


def _load_json_list(raw: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _load_json_dict(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}
