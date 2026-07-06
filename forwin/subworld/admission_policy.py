from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.canon_names import is_plausible_person_name
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import ContinuityIssue
from forwin.protocol.writer import WriterOutput

SubworldAdmissionAction = Literal[
    "register_entity",
    "genericize_background_reference",
    "manual_review_required",
]

_SUBWORLD_ADMISSION_ISSUES = {
    "subworld_admission_missing_canon_entity",
    "subworld_admission_unauthorized_new_entity",
    "sub_world_unknown_named_entity",
    "subworld_admission",
}


class SubworldAdmissionDecision(BaseModel):
    action: SubworldAdmissionAction
    entity_name: str = ""
    entity_kind: str = "character"
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    replacement: str = ""
    manual_actions: list[str] = Field(default_factory=list)


class SubworldAdmissionPolicy:
    def classify(
        self,
        *,
        issue: ContinuityIssue,
        writer_output: WriterOutput,
        chapter_goals: list[Any],
        chapter_task_contract: list[Any],
        chapter_experience_plan: ChapterExperiencePlan,
        existing_entities: list[Any],
        book_state_snapshot: dict[str, Any],
    ) -> SubworldAdmissionDecision:
        entity_name = _issue_entity_name(issue)
        entity_kind = _entity_kind_for_name(writer_output, entity_name)
        evidence_refs = list(issue.evidence_refs or [])
        issue_kind = str(issue.issue_type or issue.rule_name or "").strip()
        if issue_kind not in _SUBWORLD_ADMISSION_ISSUES and not issue_kind.startswith("subworld_admission"):
            return _manual(entity_name, entity_kind, "unsupported-subworld-admission-issue", evidence_refs)
        if not entity_name:
            return _manual("", entity_kind, "missing-entity-name", evidence_refs)

        if _is_existing_entity(entity_name, existing_entities) or issue_kind == "subworld_admission_missing_canon_entity":
            return SubworldAdmissionDecision(
                action="register_entity",
                entity_name=entity_name,
                entity_kind=entity_kind,
                reason="known entity missing from current subworld admission",
                evidence_refs=evidence_refs,
            )

        if _mentioned_in_plan(
            entity_name,
            chapter_goals=chapter_goals,
            chapter_task_contract=chapter_task_contract,
            chapter_experience_plan=chapter_experience_plan,
            book_state_snapshot=book_state_snapshot,
        ):
            return SubworldAdmissionDecision(
                action="register_entity",
                entity_name=entity_name,
                entity_kind=entity_kind,
                reason="entity appears in chapter plan or state context",
                evidence_refs=evidence_refs,
            )

        if _looks_like_safe_background_reference(entity_name, writer_output):
            return SubworldAdmissionDecision(
                action="genericize_background_reference",
                entity_name=entity_name,
                entity_kind=entity_kind,
                reason="unplanned background reference can be generalized without entering canon",
                evidence_refs=evidence_refs,
                replacement=_generic_subworld_reference(writer_output.body, entity_name),
            )

        return _manual(
            entity_name,
            entity_kind,
            "unplanned stateful named entity requires operator choice",
            evidence_refs,
        )


def _issue_entity_name(issue: ContinuityIssue) -> str:
    for name in issue.entity_names or []:
        normalized = str(name or "").strip()
        if normalized:
            return normalized
    match = re.search(r"[「\"]([^」\"]+)[」\"]", str(issue.description or ""))
    return str(match.group(1) if match else "").strip()


def _entity_kind_for_name(writer_output: WriterOutput, entity_name: str) -> str:
    for mention in writer_output.entity_mentions:
        if str(mention.entity_name or "").strip() == entity_name:
            kind = str(mention.entity_kind or "").strip()
            return "character" if kind in {"", "person"} else kind
    return "character"


def _is_existing_entity(entity_name: str, existing_entities: list[Any]) -> bool:
    for entity in existing_entities or []:
        names = {
            str(getattr(entity, "name", "") or "").strip(),
            str(getattr(entity, "entity_name", "") or "").strip(),
        }
        aliases = getattr(entity, "aliases", []) or []
        names.update(str(alias or "").strip() for alias in aliases)
        if entity_name in names:
            return True
    return False


def _mentioned_in_plan(
    entity_name: str,
    *,
    chapter_goals: list[Any],
    chapter_task_contract: list[Any],
    chapter_experience_plan: ChapterExperiencePlan,
    book_state_snapshot: dict[str, Any],
) -> bool:
    plan_text = "\n".join(
        [
            *[str(item) for item in chapter_goals or []],
            *[str(item) for item in chapter_task_contract or []],
            *[
                str(target.entity_name or "")
                for target in chapter_experience_plan.chapter_entry_targets
            ],
            str(book_state_snapshot or ""),
        ]
    )
    return bool(entity_name and entity_name in plan_text)


def _looks_like_safe_background_reference(entity_name: str, writer_output: WriterOutput) -> bool:
    if not entity_name:
        return False
    text = str(writer_output.body or "")
    if writer_output.new_events or writer_output.state_changes or writer_output.thread_beats:
        return False
    if "没有留下真名" in text or "未留真名" in text:
        return True
    if 2 <= len(entity_name) <= 3 and entity_name[0] in {"老", "小", "阿"}:
        return True
    return bool(is_plausible_person_name(entity_name) and _has_background_title_window(text, entity_name))


def _has_background_title_window(body: str, entity_name: str) -> bool:
    if entity_name not in body:
        return False
    index = body.find(entity_name)
    window = body[max(0, index - 12) : index + len(entity_name) + 12]
    return any(
        marker in window
        for marker in ("馆员", "工作人员", "高管", "总监", "主管", "负责人", "董事", "门口")
    )


def _generic_subworld_reference(body: str, observed: str) -> str:
    if observed in body:
        index = body.find(observed)
        marker_window = body[max(0, index - 30) : index + len(observed) + 30]
    else:
        marker_window = body
    if any(marker in marker_window for marker in ("集团", "董事", "会议", "总监", "高管", "部门")):
        return "集团高管"
    return "馆员"


def _manual(
    entity_name: str,
    entity_kind: str,
    reason: str,
    evidence_refs: list[str],
) -> SubworldAdmissionDecision:
    return SubworldAdmissionDecision(
        action="manual_review_required",
        entity_name=entity_name,
        entity_kind=entity_kind or "character",
        reason=reason,
        evidence_refs=evidence_refs,
        manual_actions=[
            "register_entity",
            "genericize_background_reference",
            "mark_intentional_cameo",
        ],
    )


__all__ = [
    "SubworldAdmissionAction",
    "SubworldAdmissionDecision",
    "SubworldAdmissionPolicy",
]
