from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from forwin.book_state import BookStateRepository
from forwin.api_schemas import (
    BandCheckpointDetail,
    BookGenesisPack,
    BookGenesisStageState,
    ChapterInfo,
    EntityInfo,
    GenerationControlInfo,
    PromptTraceInfo,
    ProjectAutomationSettings,
    ProjectDetail,
    ProjectSummary,
    ProvisionalBandDetail,
    ProvisionalChapterLedgerInfo,
    ScenarioRehearsalDetail,
    ThreadInfo,
)
from forwin.governance import (
    BandCheckpointIssueInfo,
    BlockingReasonInfo,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    ProjectGovernanceSettings,
    DecisionEventType,
    chapter_blocking_message,
    normalize_checkpoint_status,
    normalize_project_governance,
)
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.entity import Entity
from forwin.models.governance import BandCheckpoint, DecisionEvent, NarrativeConstraint
from forwin.models.phase import (
    ArcEnvelopeAnalysis,
    ArcStructureDraft,
    BandExperiencePlan,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalBandExecution,
    ProvisionalChapterLedger,
)
from forwin.models.phase4 import NPCIntentSnapshot, WorldSimulationTurn
from forwin.models.world_v4 import ScenarioRehearsalRunRow
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import PublisherUploadJob
from forwin.models.subworld import SubWorld, SubWorldRosterItem
from forwin.models.thread import PlotThread
from forwin.protocol.review import normalize_repair_scope
from forwin.state.query_helpers import (
    load_latest_active_arc_envelope_by_project,
    load_latest_arc_envelope_analysis_by_project,
    load_latest_drafts_by_plan_id,
    load_latest_provisional_band_execution_by_project,
    load_latest_replan_event_by_project,
    load_latest_rewrite_attempts_by_chapter,
    load_latest_stage_analysis_by_project,
    load_latest_world_turn_by_project,
)
from forwin.world_templates import empty_world_root


DisplayDatetime = Callable[[datetime | None], str]
_GENESIS_STAGE_ORDER = ("brief", "world", "map", "story_engine", "book_blueprint", "bootstrap")
_PROJECT_DETAIL_CHAPTER_PREVIEW_LIMIT = 60
_PROJECT_SUMMARY_CHAPTER_PREVIEW_LIMIT = 3
from .arc_snapshot import (
    _decision_timeline_by_project,
    _latest_band_checkpoint_by_project,
    _narrative_constraints_by_project,
    project_arc_snapshot_payload,
)
from .generation import build_generation_control, effective_target_total_chapters
from .genesis import _can_start_writing, _load_latest_genesis_revision_by_project, _stage_overview_from_revision
from .runtime_maps import load_project_runtime_maps, load_project_upload_stats, normalize_project_automation


def build_project_detail(
    *,
    session: Session,
    project: Project,
    display_datetime: DisplayDatetime,
    review_interval_chapters: int = 0,
) -> ProjectDetail:
    project_id = project.id
    genesis_revision = _load_latest_genesis_revision_by_project(session, [project_id]).get(project_id)
    entities = session.execute(
        select(Entity).where(Entity.project_id == project_id, Entity.is_active == True)
    ).scalars().all()
    book_state_nodes = BookStateRepository(session).list_world_nodes(project_id)
    book_state_characters = [
        EntityInfo(id=node.id, kind="character", name=node.name, description=node.description, importance=node.importance)
        for node in book_state_nodes
        if str(node.node_type) == "character" and bool(node.is_active)
    ]
    characters = book_state_characters or [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "character"
    ]
    locations = [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "location"
    ]
    book_state_factions = [
        EntityInfo(id=node.id, kind=str(node.node_type), name=node.name, description=node.description, importance=node.importance)
        for node in book_state_nodes
        if str(node.node_type) in {"faction", "organization", "family", "institution"} and bool(node.is_active)
    ]
    factions = book_state_factions or [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "faction"
    ]
    subworld_rows = session.execute(
        select(SubWorld).where(SubWorld.project_id == project_id).order_by(SubWorld.scope.asc(), SubWorld.created_at.asc())
    ).scalars().all()
    roster_rows = session.execute(
        select(SubWorldRosterItem).where(SubWorldRosterItem.project_id == project_id)
    ).scalars().all()
    roster_by_subworld: dict[str, list[SubWorldRosterItem]] = defaultdict(list)
    for row in roster_rows:
        roster_by_subworld[row.subworld_id].append(row)

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
    draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
    review_draft_ids = {
        draft_id
        for draft_id in session.execute(
            select(ChapterReview.draft_id)
            .where(ChapterReview.draft_id.in_([draft.id for draft in draft_map.values()]))
            .distinct()
        ).scalars().all()
    } if draft_map else set()
    latest_attempt_map = load_latest_rewrite_attempts_by_chapter(
        session,
        project_id,
        [int(plan.chapter_number or 0) for plan in plans],
    )
    upload_stats = load_project_upload_stats(session, [project_id]).get(project_id, {})
    chapter_infos = [
        ChapterInfo(
            chapter_number=plan.chapter_number,
            title=plan.title,
            status=plan.status,
            char_count=draft_map.get(plan.id).char_count if draft_map.get(plan.id) else 0,
            summary=draft_map.get(plan.id).summary if draft_map.get(plan.id) else "",
            has_draft=draft_map.get(plan.id) is not None,
            has_review=bool(draft_map.get(plan.id) and draft_map.get(plan.id).id in review_draft_ids),
            acceptance_mode=str(getattr(plan, "acceptance_mode", "") or ""),
            repair_attempt_count=int(getattr(plan, "repair_attempt_count", 0) or 0),
            canon_risk_level=str(getattr(plan, "canon_risk_level", "") or ""),
            latest_repair_scope=normalize_repair_scope(
                getattr(latest_attempt_map.get(plan.chapter_number), "repair_scope", "") or "",
                default="",
            ),
        )
        for plan in plans
    ]
    generated_chapter_count = sum(1 for plan in plans if draft_map.get(plan.id) is not None)
    accepted_chapter_count = sum(1 for plan in plans if plan.status == "accepted")
    needs_review_chapter_count = sum(1 for plan in plans if plan.status == "needs_review")

    runtime_maps = load_project_runtime_maps(session, [project_id])
    latest_checkpoint = _latest_band_checkpoint_by_project(session, [project_id]).get(project_id)
    governance = normalize_project_governance(project.governance_json)
    decision_timeline = _decision_timeline_by_project(session, [project_id], limit=30).get(project_id, [])
    narrative_constraints = _narrative_constraints_by_project(session, [project_id], limit=50).get(project_id, [])
    latest_stage = runtime_maps["latest_stage_map"].get(project_id)
    latest_world = runtime_maps["latest_world_map"].get(project_id)
    latest_arc_envelope = runtime_maps["latest_arc_envelope_map"].get(project_id)
    latest_arc_analysis = runtime_maps["latest_arc_analysis_map"].get(project_id)
    latest_provisional = runtime_maps["provisional_map"].get(project_id)
    latest_scenario_rehearsal = runtime_maps["scenario_rehearsal_map"].get(project_id)
    latest_arc_structure = runtime_maps["latest_arc_structure_map"].get(project_id)
    latest_band_experience = runtime_maps["latest_band_experience_map"].get(project_id)
    npc_intents = runtime_maps["recent_npc_map"].get(project_id, [])
    replan_events = runtime_maps["recent_replans_map"].get(project_id, [])
    active_subworld_ids: set[str] = set()
    if latest_band_experience is not None:
        try:
            payload = json.loads(latest_band_experience.schedule_json or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        active_subworld_ids = {
            str(item).strip()
            for item in (payload.get("active_subworld_ids") or [])
            if str(item).strip()
        }
    subworlds = [
        {
            "id": row.id,
            "name": row.name,
            "purpose": row.purpose,
            "scope": row.scope,
            "status": row.status,
            "active_in_current_band": row.id in active_subworld_ids,
            "core_cast": [
                item.display_name
                for item in roster_by_subworld.get(row.id, [])
                if item.is_core and str(item.display_name or "").strip()
            ],
            "planned_slot_count": sum(
                1 for item in roster_by_subworld.get(row.id, []) if item.status == "planned_slot"
            ),
        }
        for row in subworld_rows
    ]
    generation_control = build_generation_control(
        plans=plans,
        latest_replan=replan_events[0] if replan_events else None,
        review_interval_chapters=governance.review_interval_chapters or review_interval_chapters,
        latest_band_checkpoint=latest_checkpoint,
        decision_events=decision_timeline,
        future_constraints_enabled=governance.future_constraints_enabled,
    )
    has_planned_future_arc = session.execute(
        select(ArcPlanVersion.id)
        .where(
            ArcPlanVersion.project_id == project_id,
            ArcPlanVersion.status == "planned",
        )
        .limit(1)
    ).scalar_one_or_none() is not None
    if (
        str(getattr(project, "creation_status", "") or "").strip() == "writing"
        and has_planned_future_arc
        and not generation_control.pending_review_chapters
        and not generation_control.drafted_chapters
    ):
        generation_control.can_resume = True
        if generation_control.plan_state == "completed":
            generation_control.plan_state = "in_progress"
        if not generation_control.next_gate:
            generation_control.next_gate = "next_arc_ready"

    return ProjectDetail(
        id=project.id,
        title=project.title,
        premise=project.premise,
        genre=project.genre,
        setting_summary=project.setting_summary,
        target_total_chapters=effective_target_total_chapters(project, len(plans)),
        creation_status=str(getattr(project, "creation_status", "") or "legacy"),
        active_genesis_revision_id=str(getattr(project, "active_genesis_revision_id", "") or ""),
        genesis_stage_overview=_stage_overview_from_revision(genesis_revision),
        can_start_writing=_can_start_writing(project, genesis_revision),
        chapter_count=len(plans),
        generated_chapter_count=generated_chapter_count,
        accepted_chapter_count=accepted_chapter_count,
        needs_review_chapter_count=needs_review_chapter_count,
        upload_task_count=int(upload_stats.get("upload_task_count", 0) or 0),
        uploaded_chapter_count=int(upload_stats.get("uploaded_chapter_count", 0) or 0),
        automation=normalize_project_automation(project.automation_json),
        governance=governance,
        characters=characters,
        locations=locations,
        factions=factions,
        subworlds=subworlds,
        threads=thread_infos,
        chapters=chapter_infos[:_PROJECT_DETAIL_CHAPTER_PREVIEW_LIMIT],
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
            latest_arc_structure,
            latest_band_experience,
            latest_scenario_rehearsal,
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
        generation_control=generation_control,
        latest_band_checkpoint=generation_control.latest_band_checkpoint,
        blocking_reason=generation_control.blocking_reason,
        next_gate=generation_control.next_gate,
        decision_timeline=decision_timeline,
        narrative_constraints=narrative_constraints,
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


__all__ = [
    'build_project_detail',
]
