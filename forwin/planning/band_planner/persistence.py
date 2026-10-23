"""Write a validated band plan: its band_plans version, the chapter plan rows in place, and their experience plans."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.persistence import ExperiencePersistence
from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.band_planner.inputs import BandPlanInputs
from forwin.planning.band_planner.types import BandPlanDraft, ChapterPlanDraft
from forwin.planning.band_planner.validation import expected_span_minutes, response_carriers
from forwin.planning.future_plan_audit.patches import FuturePlanPatchMixin
from forwin.protocol.subworld import ChapterEntryTarget
from forwin.state.updater import StateUpdater
from forwin.world_engine.conflict_repository import PLAN_PATCH_REF_PREFIX, WorldConflictRepository
from forwin.world_engine.conflict_resolution import PLOT_RESPONSE_PATCH_TYPE, record_plot_response_patch
from forwin.world_engine.superseded_resolutions import PLAN_PATCH_REVERT_TYPE


# The admission rule band-planned chapters carry: named characters come from the plan's onstage refs.
ENTITY_ADMISSION_RULE = "strict_named_character"


@dataclass(frozen=True)
class PersistedBandPlan:
    band_plan: BandPlanRow
    # The rows written, in chapter order.
    plans: list[ChapterPlan]


def persist_band_plan(
    session: Session,
    *,
    project_id: str,
    arc_plan: ArcPlanVersion,
    beat_sheet_id: str,
    draft: BandPlanDraft,
    rows: Mapping[int, ChapterPlan],
    inputs: BandPlanInputs,
    pending_conflicts: Sequence[WorldPlanConflictRow],
    notes: Sequence[str],
) -> PersistedBandPlan:
    """Write the band plan in the caller's transaction; the caller holds the project row lock.

    ``rows`` are the band's existing plan rows; a drafted chapter's row is rewritten in place and its still-active
    plan patches applied again. ``pending_conflicts`` are the locked plot responses still waiting: each gets a
    plot-response patch on the chapter that carries it, listed in its result_refs.
    """
    band_plan = _new_band_plan_version(session, project_id=project_id, arc_plan_id=arc_plan.id,
                                       beat_sheet_id=beat_sheet_id, draft=draft, rows=rows, notes=notes)
    updater = StateUpdater(session)
    experience = ExperiencePersistence()
    if inputs.schedule_is_new:
        experience.save_band_experience_plan(session=session, project_id=project_id, arc_id=arc_plan.id,
                                             schedule=inputs.schedule)
        experience.save_trope_usage_records(session=session, project_id=project_id, arc_id=arc_plan.id,
                                            schedule=inputs.schedule)
    plans: dict[int, ChapterPlan] = {}
    for chapter in sorted(draft.chapters, key=lambda item: item.chapter_number):
        existing = rows.get(chapter.chapter_number)
        plan = existing or updater.create_chapter_plan(
            project_id, arc_plan.id, chapter.chapter_number, chapter.title, chapter.one_line, chapter.goals,
        )
        updater.update_chapter_plan(
            plan, arc_plan_id=arc_plan.id, band_plan_id=band_plan.id, title=chapter.title,
            one_line=chapter.one_line, goals=chapter.goals, pov_ref=chapter.pov_ref,
            onstage_refs=list(chapter.onstage_refs), location_refs=list(chapter.location_refs),
            world_bindings=[item.model_dump(mode="json") for item in chapter.world_bindings],
            reveal_directives=[item.model_dump(mode="json") for item in chapter.reveal_directives],
            hint_directives=[item.model_dump(mode="json") for item in chapter.hint_directives],
            intended_changes=[item.model_dump(mode="json") for item in chapter.intended_changes],
            milestone_ids=list(chapter.milestone_ids),
            expected_span_minutes=(expected_span_minutes(chapter, inputs.calendar, inputs.lexicon)
                                   if inputs.calendar is not None else None),
            hook=chapter.hook,
        )
        experience_plan = ChapterExperiencePlanner().derive_chapter_experience_plan(
            chapter_number=chapter.chapter_number, experience=inputs.experience, schedule=inputs.schedule,
            chapter_plan=plan, calibration=inputs.calibration,
        ).model_copy(update={
            "active_subworld_ids": _location_subworlds(inputs.runtime, chapter.location_refs),
            "chapter_entry_targets": _entry_targets(inputs.runtime, chapter),
            "entity_admission_rule": ENTITY_ADMISSION_RULE,
        })
        experience.save_chapter_experience_plan(chapter_plan=plan, experience_plan=experience_plan)
        if existing is not None:
            reapply_active_patches(session, plan)
        session.add(plan)
        plans[chapter.chapter_number] = plan
    conflicts = WorldConflictRepository(session)
    carriers = response_carriers(draft.chapters, inputs.offered.pending)
    for row in pending_conflicts:
        carrier = carriers.get(row.id)
        if carrier is None:
            continue
        patch_id = record_plot_response_patch(session, row, plans[carrier.chapter_number])
        conflicts.attach_result_refs(row, [f"{PLAN_PATCH_REF_PREFIX}{patch_id}"])
    session.flush()
    return PersistedBandPlan(band_plan=band_plan, plans=[plans[number] for number in sorted(plans)])


def reapply_active_patches(session: Session, plan: ChapterPlan) -> list[str]:
    """Apply again, oldest first, the plan's patches still in force; returns their ids.

    A patch is in force unless a ``plan_patch_revert`` took it back or the conflict it answers is no longer
    resolved. Applying is idempotent, so a goal a patch already put back is not doubled.
    """
    patches = NarrativeObligationRepository(session).list_patches_for_plan(plan.project_id, target_plan_id=plan.id)
    reverted = {str(patch.metadata.get("reverted_plan_patch_id") or "")
                for patch in patches if patch.patch_type == PLAN_PATCH_REVERT_TYPE}
    conflict_ids = {conflict_id for patch in patches if (conflict_id := str(patch.metadata.get("conflict_id") or ""))}
    resolved = set(session.scalars(select(WorldPlanConflictRow.id).where(
        WorldPlanConflictRow.id.in_(sorted(conflict_ids)), WorldPlanConflictRow.status == "resolved",
    ))) if conflict_ids else set()
    applied: list[str] = []
    for patch in patches:
        conflict_id = str(patch.metadata.get("conflict_id") or "")
        if patch.patch_type == PLAN_PATCH_REVERT_TYPE or patch.id in reverted or (conflict_id and conflict_id not in resolved):
            continue
        if patch.patch_type == PLOT_RESPONSE_PATCH_TYPE:
            FuturePlanPatchMixin.apply_form_plan_patch(plan, patch)
        else:
            FuturePlanPatchMixin().apply_plan_patch(plan, patch)
        applied.append(patch.id)
    return applied


def _new_band_plan_version(
    session: Session, *, project_id: str, arc_plan_id: str, beat_sheet_id: str, draft: BandPlanDraft,
    rows: Mapping[int, ChapterPlan], notes: Sequence[str],
) -> BandPlanRow:
    """The band's next version as the active one; the version it replaces becomes superseded.

    Its plan_json holds the whole band: the draft's chapters, the replaced version's other chapters, and an entry
    from its plan row for any other chapter of the band that has one (a band first planned around written rows).
    """
    band = draft.band
    same_band = (BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == arc_plan_id,
                 BandPlanRow.band_index == band.band_index)
    active = session.scalar(select(BandPlanRow).where(*same_band, BandPlanRow.status == "active")
                            .order_by(BandPlanRow.version.desc()).limit(1))
    latest = session.scalar(select(func.max(BandPlanRow.version)).where(*same_band))
    session.execute(update(BandPlanRow).where(*same_band, BandPlanRow.status == "active").values(status="superseded")
                    .execution_options(synchronize_session="fetch"))
    planned = {chapter.chapter_number for chapter in draft.chapters}
    kept = [chapter for chapter in (json.loads(active.plan_json).get("chapters", []) if active is not None else [])
            if chapter.get("chapter_number") not in planned]
    listed = planned | {chapter.get("chapter_number") for chapter in kept}
    kept += [_row_entry(plan) for number, plan in sorted(rows.items())
             if number not in listed and band.chapter_start <= number <= band.chapter_end]
    chapters = sorted([*kept, *(chapter.model_dump(mode="json") for chapter in draft.chapters)],
                      key=lambda chapter: chapter["chapter_number"])
    row = BandPlanRow(
        project_id=project_id, arc_plan_id=arc_plan_id, beat_sheet_id=beat_sheet_id, band_index=band.band_index,
        chapter_start=band.chapter_start, chapter_end=band.chapter_end, status="active",
        version=int(latest or 0) + 1, replan_count=active.replan_count + 1 if active is not None else 0,
        plan_json=json.dumps({
            "band": band.model_dump(mode="json"), "chapters": chapters,
            "planned_chapters": sorted(planned), "notes": list(notes),
        }, ensure_ascii=False),
    )
    session.add(row)
    session.flush()
    return row


def _row_entry(plan: ChapterPlan) -> dict:
    """A band plan entry for a chapter planned outside this band plan: its plan row's own fields."""
    return {
        "chapter_number": plan.chapter_number, "title": plan.title, "one_line": plan.one_line,
        "goals": json.loads(plan.goals_json or "[]"), "pov_ref": plan.pov_ref,
        **{field: json.loads(getattr(plan, f"{field}_json") or "[]") for field in (
            "onstage_refs", "location_refs", "world_bindings", "reveal_directives", "hint_directives",
            "intended_changes", "milestone_ids",
        )},
        "expected_span_minutes": plan.expected_span_minutes, "hook": plan.hook,
    }


def _location_subworlds(runtime: BookStateRuntime, location_refs: Sequence[str]) -> list[str]:
    nodes = [runtime.map.nodes_by_id.get(ref) for ref in location_refs]
    return list(dict.fromkeys(node.subworld_id for node in nodes if node is not None and node.subworld_id))


def _entry_targets(runtime: BookStateRuntime, chapter: ChapterPlanDraft) -> list[ChapterEntryTarget]:
    """The chapter's onstage actors as its entry targets, each in the subworld of its current location."""
    targets = []
    for ref in chapter.onstage_refs:
        node = runtime.world.get_node(ref)
        location = runtime.map.nodes_by_id.get(resolve_world_node_location_id(runtime.world, ref))
        targets.append(ChapterEntryTarget(
            chapter_hint=chapter.chapter_number, entity_name=(node.name if node is not None else "") or ref,
            subworld_id=location.subworld_id if location is not None else "",
        ))
    return targets


__all__ = ["ENTITY_ADMISSION_RULE", "PersistedBandPlan", "persist_band_plan", "reapply_active_patches"]


def plan_fingerprint(plan: ChapterPlan) -> str:
    """Stable revision of every chapter-plan input used by writing and review."""
    import hashlib

    payload = {
        "chapter_plan_id": str(plan.id or ""),
        "arc_plan_id": str(plan.arc_plan_id or ""),
        "chapter_number": int(plan.chapter_number or 0),
        "title": str(plan.title or ""),
        "one_line": str(plan.one_line or ""),
        "band_plan_id": plan.band_plan_id,
        "pov_ref": str(plan.pov_ref or ""),
        "expected_span_minutes": plan.expected_span_minutes,
        "hook": str(plan.hook or ""),
    }
    for field in (
        "goals_json", "task_contract_json", "experience_plan_json", "onstage_refs_json",
        "location_refs_json", "world_bindings_json", "reveal_directives_json",
        "hint_directives_json", "intended_changes_json", "milestone_ids_json",
    ):
        raw = getattr(plan, field) or ("{}" if field == "experience_plan_json" else "[]")
        try:
            payload[field] = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            payload[field] = raw
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
