from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.models import new_id
from forwin.models.entity import Entity
from forwin.models.project import ChapterPlan
from forwin.models.subworld import SubWorldRosterItem
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import ContinuityIssue, RepairInstruction
from forwin.protocol.subworld import ChapterEntryTarget
from forwin.protocol.writer import WriterOutput
from forwin.subworld.admission_policy import SubworldAdmissionDecision, SubworldAdmissionPolicy


class SubworldAdmissionPatchResult(BaseModel):
    decision: SubworldAdmissionDecision
    design_patch: dict[str, object] = Field(default_factory=dict)
    updated_plan: ChapterExperiencePlan
    replacements: dict[str, str] = Field(default_factory=dict)
    requires_writer_rewrite: bool = False
    failure_reason: str = ""


def apply_subworld_admission_patch(
    *,
    session: Session | None,
    project_id: str,
    chapter_plan: ChapterPlan,
    writer_output: WriterOutput,
    repair_instruction: RepairInstruction,
    current_plan: ChapterExperiencePlan,
    active_subworld_ids: list[str],
    context: Any,
    protected_names: set[str],
) -> SubworldAdmissionPatchResult:
    issue = _issue_from_instruction(repair_instruction)
    chapter_goals = _load_json_list(getattr(chapter_plan, "goals_json", "[]"))
    chapter_task_contract = _load_json_list(getattr(chapter_plan, "task_contract_json", "[]"))
    existing_entities = _load_existing_entities(session, project_id)
    decision = SubworldAdmissionPolicy().classify(
        issue=issue,
        writer_output=writer_output,
        chapter_goals=chapter_goals,
        chapter_task_contract=chapter_task_contract,
        chapter_experience_plan=current_plan,
        existing_entities=existing_entities,
        book_state_snapshot={},
    )
    if decision.entity_name in protected_names and decision.action == "genericize_background_reference":
        decision = SubworldAdmissionDecision(
            action="manual_review_required",
            entity_name=decision.entity_name,
            entity_kind=decision.entity_kind,
            reason="protected canon entity cannot be genericized",
            evidence_refs=decision.evidence_refs,
            manual_actions=["register_entity", "mark_intentional_cameo"],
        )

    if decision.action == "register_entity":
        updated_plan = _plan_with_entry_target(
            current_plan,
            entity_name=decision.entity_name,
            subworld_id=_selected_subworld_id(active_subworld_ids, current_plan),
        )
        chapter_plan.experience_plan_json = json.dumps(
            updated_plan.model_dump(mode="json"),
            ensure_ascii=False,
        )
        if session is not None:
            _ensure_durable_admission_rows(
                session=session,
                project_id=project_id,
                chapter_number=int(chapter_plan.chapter_number or 0),
                entity_name=decision.entity_name,
                entity_kind=decision.entity_kind,
                subworld_id=_selected_subworld_id(active_subworld_ids, current_plan),
            )
        return SubworldAdmissionPatchResult(
            decision=decision,
            updated_plan=updated_plan,
            design_patch={
                "subworld_admission_action": decision.action,
                "entity_name": decision.entity_name,
                "entity_kind": decision.entity_kind,
                "evidence_refs": list(decision.evidence_refs),
            },
        )

    if decision.action == "genericize_background_reference":
        replacement = decision.replacement or "馆员"
        return SubworldAdmissionPatchResult(
            decision=decision,
            updated_plan=current_plan,
            replacements={decision.entity_name: replacement},
            design_patch={
                "subworld_admission_action": decision.action,
                "entity_name": decision.entity_name,
                "replacement": replacement,
                "evidence_refs": list(decision.evidence_refs),
            },
        )

    return SubworldAdmissionPatchResult(
        decision=decision,
        updated_plan=current_plan,
        failure_reason=decision.reason or "manual-review-required",
        design_patch={
            "subworld_admission_action": decision.action,
            "entity_name": decision.entity_name,
            "manual_actions": list(decision.manual_actions),
            "evidence_refs": list(decision.evidence_refs),
        },
    )


def _issue_from_instruction(repair_instruction: RepairInstruction) -> ContinuityIssue:
    raw_issue = repair_instruction.design_patch.get("subworld_admission_issue")
    if isinstance(raw_issue, ContinuityIssue):
        return raw_issue
    if isinstance(raw_issue, dict):
        return ContinuityIssue.model_validate(raw_issue)
    entity_name = _entity_from_must_fix(repair_instruction.must_fix)
    return ContinuityIssue(
        rule_name="sub_world_unknown_named_entity",
        issue_type="subworld_admission_unauthorized_new_entity",
        severity="error",
        description=(repair_instruction.must_fix[0] if repair_instruction.must_fix else "subworld admission issue"),
        entity_names=[entity_name] if entity_name else [],
        evidence_refs=list(repair_instruction.evidence_refs),
    )


def _entity_from_must_fix(must_fix: list[str]) -> str:
    for item in must_fix or []:
        text = str(item or "")
        for open_marker, close_marker in (("「", "」"), ('"', '"')):
            if open_marker in text and close_marker in text:
                start = text.find(open_marker) + len(open_marker)
                end = text.find(close_marker, start)
                if end > start:
                    return text[start:end].strip()
    return ""


def _load_json_list(raw: object) -> list[Any]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _load_existing_entities(session: Session | None, project_id: str) -> list[Entity]:
    if session is None:
        return []
    return list(
        session.query(Entity)
        .filter(Entity.project_id == project_id, Entity.is_active == True)  # noqa: E712
        .all()
    )


def _plan_with_entry_target(
    current_plan: ChapterExperiencePlan,
    *,
    entity_name: str,
    subworld_id: str,
) -> ChapterExperiencePlan:
    targets = list(current_plan.chapter_entry_targets)
    if not any(str(target.entity_name or "") == entity_name for target in targets):
        targets.append(
            ChapterEntryTarget(
                entity_name=entity_name,
                subworld_id=subworld_id,
                role_hint="subworld admission repair",
            )
        )
    active_ids = list(current_plan.active_subworld_ids)
    if subworld_id and subworld_id not in active_ids:
        active_ids.append(subworld_id)
    return current_plan.model_copy(
        update={
            "chapter_entry_targets": targets,
            "active_subworld_ids": active_ids,
        }
    )


def _selected_subworld_id(active_subworld_ids: list[str], current_plan: ChapterExperiencePlan) -> str:
    for subworld_id in active_subworld_ids or []:
        if str(subworld_id or "").strip():
            return str(subworld_id).strip()
    for subworld_id in current_plan.active_subworld_ids:
        if str(subworld_id or "").strip():
            return str(subworld_id).strip()
    return ""


def _ensure_durable_admission_rows(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    entity_name: str,
    entity_kind: str,
    subworld_id: str,
) -> None:
    entity = (
        session.query(Entity)
        .filter(Entity.project_id == project_id, Entity.name == entity_name)
        .one_or_none()
    )
    if entity is None:
        entity = Entity(
            id=new_id(),
            project_id=project_id,
            kind="character" if entity_kind in {"", "person"} else entity_kind,
            name=entity_name,
            description="Created by subworld admission repair.",
            importance=5,
            created_at_chapter=chapter_number,
            is_active=True,
        )
        session.add(entity)
        session.flush()
    if subworld_id:
        existing = (
            session.query(SubWorldRosterItem)
            .filter(
                SubWorldRosterItem.project_id == project_id,
                SubWorldRosterItem.subworld_id == subworld_id,
                SubWorldRosterItem.display_name == entity_name,
            )
            .one_or_none()
        )
        if existing is None:
            session.add(
                SubWorldRosterItem(
                    id=new_id(),
                    project_id=project_id,
                    subworld_id=subworld_id,
                    entity_id=entity.id,
                    entity_kind="character" if entity_kind in {"", "person"} else entity_kind,
                    display_name=entity_name,
                    role_hint="subworld admission repair",
                    status="activated_named",
                    activation_chapter=chapter_number,
                    metadata_json=json.dumps(
                        {
                            "source": "subworld_admission_patch",
                            "auto_repair": True,
                        },
                        ensure_ascii=False,
                    ),
                )
            )


__all__ = [
    "SubworldAdmissionPatchResult",
    "apply_subworld_admission_patch",
]
