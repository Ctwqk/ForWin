from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import ContinuityIssue
from forwin.protocol.writer import WriterOutput

SubworldAdmissionAction = Literal[
    "register_entity",
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

        if _looks_like_noncast_remains_reference(entity_name):
            return _manual(
                entity_name,
                entity_kind,
                "non-cast remains reference requires entity registrar background decision",
                evidence_refs,
            )

        if _looks_like_role_or_status_label(entity_name):
            return _manual(
                entity_name,
                entity_kind,
                "role or status label requires entity registrar background decision",
                evidence_refs,
            )

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


def _looks_like_noncast_remains_reference(entity_name: str) -> bool:
    text = str(entity_name or "").strip()
    if not text:
        return False
    return text in {"尸体", "遗体", "躯体", "死者", "遇难者", "遗骸"} or any(
        text.endswith(suffix)
        for suffix in ("尸体", "遗体", "躯体", "死者", "遇难者", "遗骸")
    )


def _looks_like_role_or_status_label(entity_name: str) -> bool:
    text = str(entity_name or "").strip()
    if not text:
        return False
    if _looks_like_status_label_reference(text):
        return True
    return bool(
        text.endswith("买家")
        or "权限买家" in text
        or text.endswith("卖家")
        or "权限卖家" in text
        or text.endswith("权限者")
        or text.endswith("持有者")
    )


def _looks_like_status_label_reference(entity_name: str) -> bool:
    text = str(entity_name or "").strip()
    if not text:
        return False
    if not re.fullmatch(r"[\u4e00-\u9fff]{1,8}(?:[-_－—][\u4e00-\u9fff]{1,8})+", text):
        return False
    suffix = re.split(r"[-_－—]", text)[-1]
    return suffix in {
        "活跃",
        "已故",
        "在线",
        "离线",
        "冻结",
        "失效",
        "待审",
        "复核中",
        "未知",
        "匿名",
    }


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
            "record_background_generic_decision",
            "mark_intentional_cameo",
        ],
    )


__all__ = [
    "SubworldAdmissionAction",
    "SubworldAdmissionDecision",
    "SubworldAdmissionPolicy",
]
