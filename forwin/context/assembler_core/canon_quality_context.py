"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import func, select

from forwin.audit.events import DecisionEventType
from forwin.canon_names import extract_candidate_character_names
from forwin.canon_quality.rule_profile import CanonGlossary
from forwin.models.audit import DecisionEvent
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan

logger = logging.getLogger(__name__)

_MAP_CONTEXT_NEIGHBOR_LIMIT = 8
_MAP_CONTEXT_REVIEW_GRAPH_NODE_LIMIT = 256
_MAP_CONTEXT_REVIEW_GRAPH_EDGE_LIMIT = 512


def _build_canon_quality_context(
    *,
    session,
    project_id: str,
    chapter_number: int,
    target_total_chapters: int,
    chapter_title: str = "",
    chapter_summary: str = "",
) -> dict[str, Any]:
    is_final_chapter = _is_final_chapter_for_context(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        target_total_chapters=target_total_chapters,
        title=chapter_title,
        summary=chapter_summary,
    )
    base = {
        "target_total_chapters": int(target_total_chapters or 0),
        "is_final_chapter": is_final_chapter,
        "canon_glossary": CanonGlossary().model_dump(mode="json"),
        "countdown_rule_profiles": {},
        "countdown_constraints": [],
        "invariant_constraints": [],
        "character_state_constraints": [],
        "open_signals": [],
        "operator_retry_constraints": [],
        "active_narrative_obligations": [],
        "active_structural_patch_debt": [],
        "future_plan_audit_summary": {},
    }
    if session is None:
        return base
    try:
        from forwin.canon_quality.repository import CanonQualityRepository
        from forwin.narrative_obligations.repository import (
            NarrativeObligationRepository,
        )
        from forwin.planning.future_plan_audit import FuturePlanAuditRepository

        repo = CanonQualityRepository(session)
        obligation_repo = NarrativeObligationRepository(session)
        future_plan_audit_repo = FuturePlanAuditRepository(session)
        entries = repo.list_countdown_entries(
            project_id,
            before_chapter=int(chapter_number or 0),
            include_details=True,
        )
        latest_by_key: dict[str, dict[str, Any]] = {}
        for entry in entries:
            key = str(entry.get("countdown_key") or "").strip()
            if not key:
                continue
            remaining = entry.get("normalized_remaining_minutes")
            if remaining is None:
                continue
            if (
                (_truthy(entry.get("is_resolution_event")) or str(entry.get("status") or "") == "resolved")
                and int(remaining or 0) <= 0
            ):
                latest_by_key[key] = entry
                continue
            latest_by_key[key] = entry
        memory_entry = latest_by_key.get("memory_reset")
        main_entry = latest_by_key.get("main")
        if memory_entry is not None and main_entry is not None:
            memory_chapter = int(memory_entry.get("chapter_number") or 0)
            main_chapter = int(main_entry.get("chapter_number") or 0)
            memory_remaining = int(memory_entry.get("normalized_remaining_minutes") or 0)
            if memory_chapter >= main_chapter or memory_remaining <= 180:
                latest_by_key.pop("main", None)
        countdown_constraints = [
            {
                "countdown_key": key,
                "label": str(item.get("label") or key),
                "latest_remaining_minutes": int(item.get("normalized_remaining_minutes") or 0),
                "latest_chapter": int(item.get("chapter_number") or 0),
                "raw_mention": str(item.get("raw_mention") or ""),
                "status": str(item.get("status") or ""),
            }
            for key, item in sorted(latest_by_key.items())
            if int(item.get("normalized_remaining_minutes") or 0) >= 0
        ]
        invariant_constraints = [
            *_invariant_constraints_from_countdowns(countdown_constraints),
            *_book_state_rule_invariant_constraints(
                session=session,
                project_id=project_id,
                before_chapter=int(chapter_number or 0),
            ),
            *_accepted_future_chapter_anchor_constraints(
                session=session,
                project_id=project_id,
                chapter_number=int(chapter_number or 0),
            ),
        ]
        latest_custody_by_character: dict[str, dict[str, Any]] = {}
        for transition in repo.list_character_transitions(
            project_id,
            before_chapter=int(chapter_number or 0),
        ):
            if str(transition.get("transition_type") or "") != "custody_state":
                continue
            character_name = str(transition.get("character_name") or "").strip()
            if not character_name:
                continue
            latest_custody_by_character[character_name] = transition
        for transition in _recent_canon_custody_constraints(
            session=session,
            project_id=project_id,
            before_chapter=int(chapter_number or 0),
        ):
            character_name = str(transition.get("character_name") or "").strip()
            if not character_name:
                continue
            existing = latest_custody_by_character.get(character_name)
            if existing is not None and int(existing.get("chapter_number") or 0) > int(transition.get("chapter_number") or 0):
                continue
            latest_custody_by_character[character_name] = transition
        character_state_constraints = [
            {
                "character_name": character_name,
                "transition_type": "custody_state",
                "latest_state": str(item.get("to_state") or ""),
                "latest_chapter": int(item.get("chapter_number") or 0),
                "can_participate": bool(item.get("can_participate", True)),
                "evidence_refs": list(item.get("evidence_refs", []) or []),
            }
            for character_name, item in sorted(latest_custody_by_character.items())
        ]
        open_signals = [
            {
                "signal_id": signal.signal_id,
                "signal_type": signal.signal_type,
                "severity": signal.severity,
                "chapter_number": signal.chapter_number,
                "subject_key": signal.subject_key,
                "description": signal.description,
            }
            for signal in repo.list_open_signals(project_id, before_chapter=chapter_number, limit=10)
        ]
        operator_retry_constraints = _operator_retry_constraints(
            session=session,
            project_id=project_id,
            chapter_number=int(chapter_number or 0),
        )
        active_narrative_obligations = [
            {
                "id": obligation.id,
                "type": obligation.obligation_type,
                "priority": obligation.priority,
                "summary": obligation.summary,
                "deadline_chapter": obligation.deadline_chapter,
                "payoff_test": obligation.payoff_test,
                "must_resolve_now": obligation.must_resolve_now,
                "linked_plan_patch_ids": list(obligation.linked_plan_patch_ids),
                "evidence_refs": list(obligation.evidence_refs),
            }
            for obligation in obligation_repo.list_active_for_context(
                project_id,
                chapter_number=int(chapter_number or 0),
            )
        ]
        active_structural_patch_debt = [
            {
                "patch_id": patch.id,
                "scope": patch.target_scope,
                "target_arc_id": patch.target_arc_id,
                "target_band_id": patch.target_band_id,
                "affected_chapters": list(patch.affected_chapters),
                "payoff_tests": list(patch.expected_resolution_tests),
                "writer_context_injections": list(patch.writer_context_injections),
            }
            for patch in obligation_repo.list_active_structural_patches(
                project_id,
                chapter_number=int(chapter_number or 0),
            )
        ]
        future_plan_audit_summary: dict[str, Any] = {}
        try:
            recent_future_plan_audits = future_plan_audit_repo.list_recent(project_id, limit=1)
            if recent_future_plan_audits:
                latest_audit = recent_future_plan_audits[0]
                future_plan_audit_summary = {
                    "id": latest_audit.id,
                    "status": latest_audit.status,
                    "trigger_stage": latest_audit.trigger_stage,
                    "current_chapter": latest_audit.current_chapter,
                    "inspected_chapters": list(latest_audit.inspected_chapters),
                    "issues": [issue.model_dump(mode="json") for issue in latest_audit.issues[:5]],
                    "applied_plan_patch_ids": list(latest_audit.applied_plan_patch_ids),
                    "blocking_reasons": list(latest_audit.blocking_reasons),
                    "metadata": dict(latest_audit.metadata),
                }
                suppressed_keys = latest_audit.metadata.get("suppressed_prompt_constraint_keys", [])
                if isinstance(suppressed_keys, list):
                    future_plan_audit_summary["suppressed_prompt_constraint_keys"] = [
                        str(item)
                        for item in suppressed_keys
                        if str(item).strip()
                    ]
        except Exception:  # noqa: BLE001
            logger.debug("Future plan audit summary unavailable.", exc_info=True)
        return {
            **base,
            "countdown_constraints": countdown_constraints,
            "invariant_constraints": invariant_constraints,
            "character_state_constraints": character_state_constraints,
            "open_signals": open_signals,
            "operator_retry_constraints": operator_retry_constraints,
            "active_narrative_obligations": active_narrative_obligations,
            "active_structural_patch_debt": active_structural_patch_debt,
            "future_plan_audit_summary": future_plan_audit_summary,
            "suppressed_prompt_constraint_keys": list(
                future_plan_audit_summary.get("suppressed_prompt_constraint_keys", [])
            ),
        }
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to build canon quality context for project %s chapter %s",
            project_id,
            chapter_number,
        )
        return base


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _invariant_constraints_from_countdowns(countdown_constraints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in countdown_constraints:
        key = str(item.get("countdown_key") or "").strip()
        if not key:
            continue
        raw_mention = str(item.get("raw_mention") or "")
        result.append(
            {
                "invariant_key": f"countdown:{key}",
                "kind": "monotonic_numeric",
                "subject_key": key,
                "label": str(item.get("label") or key),
                "current_value": int(item.get("latest_remaining_minutes") or 0),
                "value_unit": "minutes",
                "status": _invariant_status(item.get("status")),
                "latest_chapter": int(item.get("latest_chapter") or 0),
                "constraints": {
                    "monotonic": True,
                    "cannot_increase_without_bridge": True,
                    "raw_mention": raw_mention,
                },
                "allowed_bridges": ["reset", "reopened", "branch_clock"],
            }
        )
    return result


def _accepted_future_chapter_anchor_constraints(
    *,
    session,
    project_id: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    """Keep a historical rewrite compatible with the nearest accepted canon."""

    rows = session.execute(
        select(ChapterPlan, ChapterDraft)
        .join(CanonCommitRecord, CanonCommitRecord.id == ChapterPlan.active_commit_id)
        .join(
            CandidateDraftRecord,
            CandidateDraftRecord.id == CanonCommitRecord.candidate_id,
        )
        .join(
            ChapterDraft,
            ChapterDraft.id == CandidateDraftRecord.candidate_draft_id,
        )
        .where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.status == "accepted",
            CandidateDraftRecord.canon_status == "canon",
            ChapterPlan.chapter_number > int(chapter_number or 0),
        )
        .order_by(ChapterPlan.chapter_number.asc())
        .limit(3)
    ).all()
    anchors: list[dict[str, Any]] = []
    for plan, draft in rows:
        accepted_chapter = int(plan.chapter_number or 0)
        if accepted_chapter <= 0:
            continue
        anchors.append(
            {
                "invariant_key": f"accepted_future_chapter:{accepted_chapter}",
                "kind": "accepted_future_anchor",
                "subject_key": f"chapter:{accepted_chapter}",
                "label": f"第{accepted_chapter}章已接受结果",
                "current_value": {
                    "chapter_number": accepted_chapter,
                    "title": str(plan.title or "").strip(),
                    "summary": str(draft.summary or "").strip(),
                },
                "status": "accepted",
                "latest_chapter": accepted_chapter,
                "constraints": {
                    "immutable_definition": True,
                    "historical_rewrite_anchor": True,
                    "must_remain_compatible": True,
                },
                "evidence_refs": [
                    f"accepted_chapter:{accepted_chapter}",
                    f"candidate_draft:{CandidateDraftRecord.__name__}:{accepted_chapter}",
                ],
                "source": "accepted_future_chapter",
            }
        )
    return anchors


def _operator_retry_constraints(
    *,
    session,
    project_id: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    """Surface the latest human retry reason to the next rewrite prompt."""

    if chapter_number <= 0:
        return []
    event = session.execute(
        select(DecisionEvent)
        .where(
            DecisionEvent.project_id == project_id,
            DecisionEvent.chapter_number == chapter_number,
            DecisionEvent.event_family == "audit_action",
            DecisionEvent.event_type == DecisionEventType.RETRY_ATTEMPT,
            DecisionEvent.actor_type.in_(("api", "manual_ui")),
        )
        .order_by(DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    reason = str(getattr(event, "reason", "") or "").strip()
    if not reason:
        return []
    return [
        {
            "chapter_number": chapter_number,
            "reason": reason,
            "event_id": str(getattr(event, "id", "") or ""),
        }
    ]


def _book_state_rule_invariant_constraints(
    *,
    session,
    project_id: str,
    before_chapter: int,
) -> list[dict[str, Any]]:
    from forwin.book_state.projection import BookStateProjection

    runtime = BookStateProjection(session).load_runtime_as_of(
        project_id,
        as_of_chapter=max(int(before_chapter or 0) - 1, 0),
    )
    constraints: list[dict[str, Any]] = []
    for node in runtime.world.nodes_by_id.values():
        if str(node.node_type or "") != "rule":
            continue
        state = runtime.world.get_state(node.id)
        status = str(state.get("status") or node.status or "active").strip()
        if not node.is_active or status in {"inactive", "retired", "deleted", "revoked"}:
            continue
        current_value = _canonical_rule_definition(node)
        if not current_value:
            continue
        constraints.append(
            {
                "invariant_key": f"book_state_rule:{node.id}",
                "kind": "active_rule",
                "subject_key": node.id,
                "label": str(node.name or node.id),
                "current_value": current_value,
                "value_unit": "",
                "status": "active",
                "latest_chapter": int(node.created_at_chapter or 0),
                "constraints": {
                    "immutable_definition": True,
                    "requires_explicit_bridge": True,
                },
                "allowed_bridges": ["supersede", "retcon", "revoke"],
                "evidence_refs": list(node.source_refs),
                "source": "book_state_rule",
                "payload": {
                    "node_id": node.id,
                    "aliases": list(node.aliases),
                },
            }
        )
    return sorted(constraints, key=lambda item: str(item["invariant_key"]))


def _canonical_rule_definition(node) -> dict[str, Any]:
    definition = {
        str(key): value
        for key, value in dict(node.profile or {}).items()
        if value not in (None, "", [], {})
        and _is_public_rule_definition_field(key)
    }
    metadata = dict(node.metadata or {})
    writer_state = metadata.get("writer_state")
    if isinstance(writer_state, dict):
        for key, value in writer_state.items():
            if (
                value not in (None, "", [], {})
                and _is_public_rule_definition_field(key)
            ):
                definition.setdefault(str(key), value)
    if not definition:
        fallback = str(node.description or node.summary or "").strip()
        if fallback:
            definition["description"] = fallback
    return definition


def _is_public_rule_definition_field(value: Any) -> bool:
    key = str(value or "").strip().lower()
    return bool(key) and not any(
        marker in key
        for marker in (
            "hidden",
            "secret",
            "private",
            "internal",
            "objective_truth",
        )
    )


def _invariant_status(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized in {"active", "paused", "closed", "fulfilled", "resolved", "revoked", "warning", "conflict"}:
        return normalized
    if normalized in {"consistent", "open"}:
        return "active"
    if normalized in {"blocked", "error", "failed"}:
        return "conflict"
    return "unknown"


_RECENT_CANON_CUSTODY_RELEASE_MARKERS = ("救出", "救下", "营救", "解救", "脱困", "释放", "获救")
_RECENT_CANON_POST_NAME_RELEASE_MARKERS = ("获救", "脱困", "被释放", "已释放", "被救出", "被救下", "被解救")
_RECENT_CANON_CUSTODY_CAPTURE_MARKERS = (
    "被关押",
    "被关在",
    "被关进",
    "被扣押",
    "被捕",
    "被固定",
    "被束缚",
    "被锁在",
    "关在",
    "锁在",
    "束缚",
    "固定在",
)


def _recent_canon_custody_constraints(*, session, project_id: str, before_chapter: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(CandidateDraftRecord, ChapterDraft)
        .join(ChapterDraft, ChapterDraft.id == CandidateDraftRecord.candidate_draft_id)
        .join(CanonCommitRecord, CanonCommitRecord.candidate_id == CandidateDraftRecord.id)
        .join(ChapterPlan, ChapterPlan.active_commit_id == CanonCommitRecord.id)
        .where(
            CandidateDraftRecord.project_id == project_id,
            CandidateDraftRecord.chapter_number < int(before_chapter or 0),
            CandidateDraftRecord.canon_status == "canon",
        )
        .order_by(CandidateDraftRecord.chapter_number.desc(), CandidateDraftRecord.updated_at.desc())
        .limit(3)
    ).all()
    latest_by_character: dict[str, dict[str, Any]] = {}
    for record, draft in reversed(rows):
        chapter_number = int(record.chapter_number or 0)
        text = "\n".join(part for part in (str(draft.summary or ""), str(draft.body_text or "")) if part)
        if not text:
            continue
        for character_name in _candidate_recent_canon_character_names(text):
            state = _custody_state_from_recent_text(text, character_name=character_name)
            if not state:
                continue
            latest_by_character[character_name] = {
                "character_name": character_name,
                "chapter_number": chapter_number,
                "transition_type": "custody_state",
                "to_state": state,
                "terminality": "none",
                "can_participate": True,
                "evidence_refs": [f"recent_canon:{chapter_number}"],
                "payload": {"source": "recent_canon_text"},
            }
    return [latest_by_character[name] for name in sorted(latest_by_character)]


def _candidate_recent_canon_character_names(text: str) -> set[str]:
    return extract_candidate_character_names(text)


def _custody_state_from_recent_text(text: str, *, character_name: str) -> str:
    state = ""
    start = 0
    while True:
        index = text.find(character_name, start)
        if index < 0:
            break
        before_window = text[max(0, index - 24) : index]
        after_window = text[index + len(character_name) : min(len(text), index + len(character_name) + 24)]
        before = _last_clause_fragment(before_window)
        after = _first_clause_fragment(after_window)
        after_stripped = after.lstrip()
        released = any(marker in before for marker in _RECENT_CANON_CUSTODY_RELEASE_MARKERS) or any(
            after_stripped.startswith(marker) for marker in _RECENT_CANON_POST_NAME_RELEASE_MARKERS
        )
        captured = any(marker in before for marker in _RECENT_CANON_CUSTODY_CAPTURE_MARKERS) or any(
            after_stripped.startswith(marker) for marker in _RECENT_CANON_CUSTODY_CAPTURE_MARKERS
        )
        if released:
            state = "free"
        elif captured:
            state = "captured"
        start = index + max(1, len(character_name))
    return state


def _last_clause_fragment(text: str) -> str:
    return re.split(r"[，,。；;！!？?\n]+", str(text or ""))[-1]


def _first_clause_fragment(text: str) -> str:
    return re.split(r"[，,。；;！!？?\n]+", str(text or ""))[0]


def _is_final_chapter_for_context(
    *,
    session,
    project_id: str,
    chapter_number: int,
    target_total_chapters: int,
    title: str = "",
    summary: str = "",
) -> bool:
    current = int(chapter_number or 0)
    if current <= 0:
        return False
    target_total = int(target_total_chapters or 0)
    if target_total and current >= target_total:
        return True
    if target_total and current < target_total:
        return False
    if session is None or not _looks_like_final_chapter_label(title=title, summary=summary):
        return False
    try:
        max_materialized = session.execute(
            select(func.max(ChapterPlan.chapter_number)).where(ChapterPlan.project_id == project_id)
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to infer final chapter context for project %s chapter %s",
            project_id,
            chapter_number,
        )
        return False
    return max_materialized is not None and current >= int(max_materialized or 0)


def _looks_like_final_chapter_label(*, title: str, summary: str = "") -> bool:
    text = f"{title}\n{summary}"
    return any(
        marker in text
        for marker in (
            "终章",
            "尾声",
            "大结局",
            "最终章",
            "最后一章",
            "最后一日",
            "最后一天",
            "最终决战",
            "finale",
            "Finale",
        )
    )


__all__ = [
    '_build_canon_quality_context',
    '_book_state_rule_invariant_constraints',
    '_truthy',
    '_recent_canon_custody_constraints',
    '_candidate_recent_canon_character_names',
    '_custody_state_from_recent_text',
    '_last_clause_fragment',
    '_first_clause_fragment',
    '_is_final_chapter_for_context',
    '_looks_like_final_chapter_label',
]
