"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import json
import logging

from forwin.protocol.context import (
    ArcEnvelopeView,
    AudienceHintView,
    ChapterContextPack,
    NPCIntentView,
    TimelineSnapshot,
    WorldPressureView,
)

logger = logging.getLogger(__name__)


def _build_genesis_map_overview(map_atlas: dict, runtime_region_drafts: list[dict]) -> str:
    parts: list[str] = []
    overview = str(map_atlas.get("overview", "") or "").strip()
    if overview:
        parts.append(overview)
    submaps = map_atlas.get("submaps") if isinstance(map_atlas.get("submaps"), list) else []
    regions = map_atlas.get("regions") if isinstance(map_atlas.get("regions"), list) else []
    nodes = map_atlas.get("nodes") if isinstance(map_atlas.get("nodes"), list) else []
    if submaps:
        submap_names = [str(item.get("name", "") or "").strip() for item in submaps if isinstance(item, dict)]
        submap_names = [item for item in submap_names if item]
        if submap_names:
            parts.append(f"Genesis 小世界：{'、'.join(submap_names[:6])}")
    if regions:
        region_lines: list[str] = []
        for region in regions[:8]:
            if not isinstance(region, dict):
                continue
            name = str(region.get("name", "") or "").strip()
            if not name:
                continue
            subworld_name = str(region.get("subworld_name", "") or "").strip()
            level = str(region.get("level", "") or "").strip()
            region_lines.append(f"{name}{f'@{subworld_name}' if subworld_name else ''}{f'·L{level}' if level else ''}")
        if region_lines:
            parts.append(f"Genesis 地区：{'、'.join(region_lines)}")
    if nodes:
        node_lines: list[str] = []
        for node in nodes[:8]:
            if not isinstance(node, dict):
                continue
            name = str(node.get("name", "") or "").strip()
            if not name:
                continue
            parent_region = str(node.get("parent_region_id", "") or "").strip()
            node_lines.append(f"{name}{f'@{parent_region}' if parent_region else ''}")
        if node_lines:
            parts.append(f"Genesis 地点：{'、'.join(node_lines)}")
    if runtime_region_drafts:
        draft_lines: list[str] = []
        for draft in runtime_region_drafts[:8]:
            if not isinstance(draft, dict):
                continue
            name = str(draft.get("name", "") or "").strip()
            if not name:
                continue
            subworld_name = str(draft.get("subworld_name", "") or "").strip()
            level = str(draft.get("level", "") or "").strip()
            draft_lines.append(f"{name}{f'@{subworld_name}' if subworld_name else ''}{f'·L{level}' if level else ''}")
        if draft_lines:
            parts.append(f"运行时地区草案：{'、'.join(draft_lines)}")
    return "；".join(part for part in parts if part)


def assemble_context(
    repo,  # StateRepository
    project_id: str,
    chapter_plan,  # ChapterPlan ORM object
) -> ChapterContextPack:
    """Build a ChapterContextPack for the writer.

    Args:
        repo: StateRepository instance
        project_id: current project ID
        chapter_plan: ChapterPlan ORM object with chapter_number, title, one_line, goals_json

    Returns:
        ChapterContextPack ready for the writer
    """
    # 1. Get project
    project = repo.get_project(project_id)
    genesis_refs: dict[str, str] = {}
    genesis_world_overview = ""
    genesis_map_overview = ""
    genesis_story_engine_summary = ""
    genesis_map_atlas: dict = {}
    runtime_region_drafts: list[dict] = []
    genesis_getter = getattr(repo, "get_active_genesis_revision", None)
    if callable(genesis_getter):
        genesis_revision = genesis_getter(project_id)
        if genesis_revision is not None:
            try:
                genesis_pack = json.loads(getattr(genesis_revision, "pack_json", "{}") or "{}") or {}
            except (TypeError, ValueError, json.JSONDecodeError):
                genesis_pack = {}
            if isinstance(genesis_pack, dict):
                world_root = genesis_pack.get("world") if isinstance(genesis_pack.get("world"), dict) else {}
                if not world_root:
                    world_root = {
                        "world_bible": genesis_pack.get("world_bible") if isinstance(genesis_pack.get("world_bible"), dict) else {},
                        "map_atlas": genesis_pack.get("map_atlas") if isinstance(genesis_pack.get("map_atlas"), dict) else {},
                        "story_engine": genesis_pack.get("story_engine") if isinstance(genesis_pack.get("story_engine"), dict) else {},
                    }
                world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
                genesis_map_atlas = world_root.get("map_atlas") if isinstance(world_root.get("map_atlas"), dict) else {}
                story_engine = world_root.get("story_engine") if isinstance(world_root.get("story_engine"), dict) else {}
                genesis_world_overview = str(world_bible.get("overview", "") or "")
                long_arcs = story_engine.get("long_arcs") if isinstance(story_engine.get("long_arcs"), list) else []
                genesis_story_engine_summary = "；".join(str(item).strip() for item in long_arcs if str(item).strip())
                genesis_refs = {
                    "genesis_revision_id": str(getattr(genesis_revision, "id", "") or ""),
                    "genesis_revision_number": str(getattr(genesis_revision, "revision", "") or ""),
                }

    # 2. Get chapter-allowed entities with latest states. Older test doubles
    # may only expose the pre-subworld getter.
    allowed_entities_getter = getattr(repo, "get_allowed_entity_snapshots", None)
    if callable(allowed_entities_getter):
        entities = allowed_entities_getter(project_id, chapter_plan.chapter_number)
    else:
        entities = repo.get_active_entities(project_id)

    allowed_entities = [entity.name for entity in entities if entity.kind == "character"]

    # 3. Get relations narrowed to allowed entities
    relations_getter = getattr(repo, "get_active_relations")
    try:
        relations = relations_getter(project_id, entity_names=allowed_entities)
    except TypeError:
        relations = [
            relation
            for relation in relations_getter(project_id)
            if relation.source_name in allowed_entities or relation.target_name in allowed_entities
        ]

    # 4. Get active plot threads with recent beats
    threads = repo.get_active_threads(project_id)

    # 5. Get previous chapter summaries (last 3)
    summaries = repo.get_chapter_summaries(project_id, chapter_plan.chapter_number)

    # 6. Get current timeline
    timeline = repo.get_current_timeline(project_id)

    # 6.5 Get latest NPC intents / world pressure
    npc_intents_getter = getattr(repo, "get_recent_npc_intents", None)
    npc_intents = (
        npc_intents_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(npc_intents_getter)
        else []
    )
    world_pressure_getter = getattr(repo, "get_latest_world_pressure", None)
    world_pressure = (
        world_pressure_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(world_pressure_getter)
        else None
    )
    arc_envelope_getter = getattr(repo, "get_active_arc_envelope", None)
    arc_envelope_row = (
        arc_envelope_getter(project_id)
        if callable(arc_envelope_getter)
        else None
    )
    reader_promise_getter = getattr(repo, "get_reader_promise", None)
    reader_promise = (
        reader_promise_getter(project_id)
        if callable(reader_promise_getter)
        else None
    )
    arc_payoff_map_getter = getattr(repo, "get_arc_payoff_map", None)
    arc_payoff_map = (
        arc_payoff_map_getter(project_id)
        if callable(arc_payoff_map_getter)
        else None
    )
    band_schedule_getter = getattr(repo, "get_band_experience_plan_for_chapter", None)
    band_schedule = (
        band_schedule_getter(project_id, chapter_plan.chapter_number)
        if callable(band_schedule_getter)
        else None
    )
    chapter_experience_getter = getattr(repo, "get_chapter_experience_plan", None)
    chapter_experience_plan = (
        chapter_experience_getter(project_id, chapter_plan.chapter_number)
        if callable(chapter_experience_getter)
        else None
    )
    active_subworld_summary_getter = getattr(repo, "get_active_subworld_summary", None)
    active_subworlds = (
        active_subworld_summary_getter(project_id, chapter_plan.chapter_number)
        if callable(active_subworld_summary_getter)
        else []
    )
    active_subworld_region_drafts_getter = getattr(repo, "get_active_subworld_region_drafts", None)
    runtime_region_drafts = (
        active_subworld_region_drafts_getter(project_id, chapter_plan.chapter_number)
        if callable(active_subworld_region_drafts_getter)
        else []
    )
    genesis_map_overview = _build_genesis_map_overview(genesis_map_atlas, runtime_region_drafts)
    audience_hints_getter = getattr(repo, "get_audience_hints", None)
    audience_hints_raw = (
        audience_hints_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(audience_hints_getter)
        else None
    )
    chapter_task_contract_getter = getattr(repo, "get_chapter_task_contract", None)
    chapter_task_contract = (
        chapter_task_contract_getter(project_id, chapter_plan.chapter_number)
        if callable(chapter_task_contract_getter)
        else []
    )
    band_task_contract_getter = getattr(repo, "get_band_task_contract_for_chapter", None)
    band_task_contract = (
        band_task_contract_getter(project_id, chapter_plan.chapter_number)
        if callable(band_task_contract_getter)
        else []
    )
    constraints_enabled_getter = getattr(repo, "future_constraints_enabled", None)
    constraints_enabled = (
        bool(constraints_enabled_getter(project_id))
        if callable(constraints_enabled_getter)
        else True
    )
    active_constraints_getter = getattr(repo, "list_active_narrative_constraints", None)
    active_constraints = (
        active_constraints_getter(project_id, chapter_number=chapter_plan.chapter_number)
        if constraints_enabled and callable(active_constraints_getter)
        else []
    )
    next_band_summary_getter = getattr(repo, "get_next_band_summary", None)
    next_band_summary = (
        next_band_summary_getter(project_id, chapter_plan.chapter_number)
        if callable(next_band_summary_getter)
        else None
    )

    # 7. Parse chapter goals from goals_json
    try:
        goals = json.loads(chapter_plan.goals_json) if chapter_plan.goals_json else []
    except json.JSONDecodeError:
        goals = []

    # 8. Build and return pack
    return ChapterContextPack(
        project_id=project_id,
        project_title=project.title,
        premise=project.premise,
        genre=project.genre,
        setting_summary=project.setting_summary,
        genesis_context_refs=genesis_refs,
        genesis_world_overview=genesis_world_overview,
        genesis_map_overview=genesis_map_overview,
        genesis_story_engine_summary=genesis_story_engine_summary,
        chapter_number=chapter_plan.chapter_number,
        chapter_plan_title=chapter_plan.title,
        chapter_plan_one_line=chapter_plan.one_line,
        chapter_goals=goals,
        previous_chapter_summaries=summaries,
        active_entities=entities,
        active_relations=relations,
        active_threads=threads,
        timeline=timeline,
        npc_intents=npc_intents,
        world_pressure=world_pressure,
        reader_feedback=None,
        current_arc_envelope=(
            ArcEnvelopeView(
                source_policy_tier=arc_envelope_row.source_policy_tier,
                base_target_size=arc_envelope_row.base_target_size,
                base_soft_min=arc_envelope_row.base_soft_min,
                base_soft_max=arc_envelope_row.base_soft_max,
                resolved_target_size=arc_envelope_row.resolved_target_size,
                resolved_soft_min=arc_envelope_row.resolved_soft_min,
                resolved_soft_max=arc_envelope_row.resolved_soft_max,
                detailed_band_size=arc_envelope_row.detailed_band_size,
                frozen_zone_size=arc_envelope_row.frozen_zone_size,
                current_projected_size=arc_envelope_row.current_projected_size,
                current_confidence=arc_envelope_row.current_confidence,
            )
            if arc_envelope_row is not None
            else None
        ),
        audience_hints=(
            AudienceHintView(
                pacing_hints=audience_hints_raw.pacing_hints,
                clarity_hints=audience_hints_raw.clarity_hints,
                character_heat_changes=audience_hints_raw.character_heat_changes,
                risk_flags=audience_hints_raw.risk_flags,
            )
            if audience_hints_raw is not None
            else None
        ),
        reader_promise=reader_promise,
        arc_payoff_map=arc_payoff_map,
        band_delight_schedule=band_schedule,
        chapter_experience_plan=chapter_experience_plan,
        active_subworlds=active_subworlds,
        allowed_entities=allowed_entities,
        chapter_entry_targets=(
            list(chapter_experience_plan.chapter_entry_targets)
            if chapter_experience_plan is not None
            else []
        ),
        entity_admission_rule=(
            str(chapter_experience_plan.entity_admission_rule or "").strip()
            if chapter_experience_plan is not None
            else ""
        ),
        chapter_task_contract=chapter_task_contract,
        band_task_contract=band_task_contract,
        active_future_constraints=active_constraints,
        next_band_summary=next_band_summary,
    )
