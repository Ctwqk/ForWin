from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.models.audit import DecisionEvent
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.phase import BandExperiencePlan
from forwin.models.planning_control import BandCheckpoint
from forwin.models.project import ChapterPlan, Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.planning.checkpoints import (
    BandCheckpointDetail,
    BandCheckpointIssueInfo,
    NextBandSummary,
    normalize_checkpoint_status,
)
from forwin.planning.constraints import NarrativeConstraintInfo
from forwin.planning.contracts import (
    PlanTaskItem,
    is_derived_goal_control_instruction,
)
from forwin.protocol.experience import BandDelightSchedule
from forwin.protocol.review import ContinuityIssue
from forwin.protocol.state_change import (
    EventCandidate,
    StateChangeCandidate,
    ThreadBeatCandidate,
)
from forwin.protocol.writer import WriterOutput
from forwin.review.constraint_keywords import (
    constraint_keywords,
    first_unnegated_keyword,
    text_has_unnegated_keyword,
)
from forwin.review.issue_groups import issue_group_for_issue
from forwin.state.query_helpers import (
    load_candidate_reviews_by_draft_id,
    load_latest_drafts_by_plan_id,
)

if TYPE_CHECKING:
    from forwin.observability.pipeline_trace import PipelineAuditContext

_KEYWORDS = constraint_keywords()
_DEATH_KEYWORDS = _KEYWORDS.death
_REVEAL_KEYWORDS = _KEYWORDS.reveal
_RELATION_BREAK_KEYWORDS = _KEYWORDS.relation_break
_LOCATION_DESTROY_KEYWORDS = _KEYWORDS.location_destroy
_RULE_BREAK_KEYWORDS = _KEYWORDS.rule_break
_RESOURCE_CLOSURE_KEYWORDS = _KEYWORDS.resource_closure
_THREAD_CLOSURE_KEYWORDS = _KEYWORDS.thread_closure
_GROWTH_COMPLETION_KEYWORDS = _KEYWORDS.growth_completion


def chapter_combined_text(writer_output: WriterOutput) -> str:
    parts = [
        str(writer_output.title or ""),
        str(writer_output.body or ""),
        str(writer_output.end_of_chapter_summary or ""),
        *(str(event.summary or "") for event in writer_output.new_events),
        *(str(beat.description or "") for beat in writer_output.thread_beats),
        *(str(change.reason or "") for change in writer_output.state_changes),
        *(str(change.new_value or "") for change in writer_output.state_changes),
    ]
    return "\n".join(part for part in parts if part.strip())


def band_combined_text(
    *,
    chapter_bodies: Iterable[str],
    chapter_summaries: Iterable[str],
    event_summaries: Iterable[str] = (),
    beat_descriptions: Iterable[str] = (),
) -> str:
    parts = [
        *(str(item or "") for item in chapter_bodies),
        *(str(item or "") for item in chapter_summaries),
        *(str(item or "") for item in event_summaries),
        *(str(item or "") for item in beat_descriptions),
    ]
    return "\n".join(part for part in parts if part.strip())


def evaluate_task_contract(
    tasks: list[PlanTaskItem],
    *,
    combined_text: str,
    reviewer: str,
    issue_type: str,
    target_scope: str,
) -> list[ContinuityIssue]:
    issues: list[ContinuityIssue] = []
    for task in tasks:
        if _is_low_signal_derived_task(task):
            continue
        if not _task_is_satisfied(task, combined_text):
            issues.append(
                ContinuityIssue(
                    rule_name="plan_task_unfulfilled",
                    severity="warning",
                    description=f"规划任务未明显交付：{task.description or task.target_name or task.task_type}",
                    entity_names=[task.target_name] if task.target_name else [],
                    reviewer=reviewer,
                    issue_type=issue_type,
                    target_scope=target_scope,
                    issue_group=issue_group_for_issue(
                        issue_type=issue_type, rule_name="plan_task_unfulfilled"
                    ),
                    evidence_refs=[
                        f"task_type={task.task_type}",
                        f"task_source={task.source}",
                    ],
                    suggested_fix="补足该章/该 band 需要交付的规划任务，或显式调整任务合同。",
                )
            )
    return issues


def evaluate_band_obligation_contract(
    schedule: BandDelightSchedule | None,
    *,
    obligations: list[NarrativeObligation],
    band_end_chapter: int,
    reviewer: str,
    target_scope: str,
) -> list[ContinuityIssue]:
    if schedule is None:
        return []
    contract = schedule.band_obligation_contract
    if not contract.open_obligations:
        return []
    obligations_by_id = {item.id: item for item in obligations if item.id}
    issues: list[ContinuityIssue] = []
    for obligation_id in contract.must_resolve_by_band_end:
        obligation = obligations_by_id.get(obligation_id)
        if obligation is None or obligation.status in {"resolved", "waived"}:
            continue
        issues.append(
            ContinuityIssue(
                rule_name="band_obligation_unresolved",
                severity="error",
                description=f"band 结束时叙事义务仍未清偿：{obligation_id}",
                reviewer=reviewer,
                issue_type="band_obligation_completion",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(
                    issue_type="band_obligation_completion",
                    rule_name="band_obligation_unresolved",
                ),
                evidence_refs=[
                    f"obligation={obligation_id}",
                    f"deadline_chapter={obligation.deadline_chapter}",
                    f"band_end={int(band_end_chapter or 0)}",
                ],
                suggested_fix="清偿该 band-level obligation，或通过人工治理将 P2/P3 合理 carry forward。",
            )
        )
    for obligation_id in contract.allowed_carry_forward:
        obligation = obligations_by_id.get(obligation_id)
        if obligation is None or obligation.status in {"resolved", "waived"}:
            continue
        if obligation.priority in {"P0", "P1"}:
            issues.append(
                ContinuityIssue(
                    rule_name="band_obligation_invalid_carry_forward",
                    severity="error",
                    description=f"P0/P1 叙事义务不能跨 band carry forward：{obligation_id}",
                    reviewer=reviewer,
                    issue_type="band_obligation_completion",
                    target_scope=target_scope,
                    issue_group=issue_group_for_issue(
                        issue_type="band_obligation_completion",
                        rule_name="band_obligation_invalid_carry_forward",
                    ),
                    evidence_refs=[
                        f"obligation={obligation_id}",
                        f"priority={obligation.priority}",
                    ],
                    suggested_fix="将该义务改为本 band 清偿，或升级 arc/manual replan。",
                )
            )
        elif int(obligation.deadline_chapter or 0) <= int(band_end_chapter or 0):
            issues.append(
                ContinuityIssue(
                    rule_name="band_obligation_carry_forward_after_deadline",
                    severity="error",
                    description=f"叙事义务已到期，不能 carry forward：{obligation_id}",
                    reviewer=reviewer,
                    issue_type="band_obligation_completion",
                    target_scope=target_scope,
                    issue_group=issue_group_for_issue(
                        issue_type="band_obligation_completion",
                        rule_name="band_obligation_carry_forward_after_deadline",
                    ),
                    evidence_refs=[
                        f"obligation={obligation_id}",
                        f"deadline_chapter={obligation.deadline_chapter}",
                        f"band_end={int(band_end_chapter or 0)}",
                    ],
                    suggested_fix="先清偿到期义务，再进入下一 band。",
                )
            )
    return issues


def _is_low_signal_derived_task(task: PlanTaskItem) -> bool:
    if str(task.source or "") != "derived_from_goals":
        return False
    description = str(task.description or "").strip()
    return len(description) < 2 or is_derived_goal_control_instruction(description)


def evaluate_constraint_issues(
    constraints: list[NarrativeConstraintInfo],
    *,
    combined_text: str,
    state_changes: list[StateChangeCandidate],
    events: list[EventCandidate],
    thread_beats: list[ThreadBeatCandidate],
    reviewer: str,
    issue_type: str,
    target_scope: str,
) -> list[ContinuityIssue]:
    issues: list[ContinuityIssue] = []
    lower_text = combined_text.lower()
    for constraint in constraints:
        severity = _constraint_severity(constraint.level)
        entity_names = [constraint.subject_name] if constraint.subject_name else []
        evidence_refs = [
            f"constraint={constraint.id}",
            f"constraint_type={constraint.constraint_type}",
        ]
        matched, detail = _constraint_triggered(
            constraint,
            combined_text=combined_text,
            lower_text=lower_text,
            state_changes=state_changes,
            events=events,
            thread_beats=thread_beats,
        )
        if not matched:
            continue
        issues.append(
            ContinuityIssue(
                rule_name="future_constraint_violation",
                severity=severity,
                description=f"叙事约束被触发：{constraint.description or constraint.subject_name or constraint.constraint_type}",
                entity_names=entity_names,
                reviewer=reviewer,
                issue_type=issue_type,
                target_scope=target_scope,
                issue_group=issue_group_for_issue(
                    issue_type=issue_type, rule_name="future_constraint_violation"
                ),
                evidence_refs=[*evidence_refs, detail],
                suggested_fix="撤回会锁死未来空间的写法，或下调/修改该约束。",
            )
        )
    return issues


def evaluate_resource_closure_risk(
    *,
    combined_text: str,
    next_band_targets: list[str],
    reviewer: str,
    target_scope: str,
) -> list[ContinuityIssue]:
    text = combined_text.strip()
    if not text:
        return []
    issues: list[ContinuityIssue] = []
    for target in next_band_targets:
        name = str(target or "").strip()
        if not name:
            continue
        if name not in text:
            continue
        if not (
            any(keyword in text for keyword in _RESOURCE_CLOSURE_KEYWORDS)
            or any(keyword in text for keyword in _GROWTH_COMPLETION_KEYWORDS)
        ):
            continue
        category = _resource_closure_category(text)
        issues.append(
            ContinuityIssue(
                rule_name="future_resource_preservation_risk",
                severity="warning",
                description=f"当前内容可能过早封闭未来资源：{name}",
                entity_names=[name],
                reviewer=reviewer,
                issue_type="future_resource_preservation",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(
                    issue_type="future_resource_preservation",
                    rule_name="future_resource_preservation_risk",
                ),
                evidence_refs=[f"target={name}", f"category={category}"],
                suggested_fix="避免把后续 band 仍可能要使用的角色、线索或关系写成彻底关闭。",
            )
        )
    return issues


def evaluate_intra_band_consistency(
    *,
    unresolved_review_chapters: list[int],
    review_fail_chapters: list[int],
    pending_checkpoint_count: int,
    reviewer: str,
    target_scope: str,
) -> list[ContinuityIssue]:
    issues: list[ContinuityIssue] = []
    if unresolved_review_chapters:
        issues.append(
            ContinuityIssue(
                rule_name="intra_band_consistency_unresolved_review",
                severity="error",
                description="band 内存在未处理 chapter review。",
                reviewer=reviewer,
                issue_type="intra_band_consistency",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="intra_band_consistency"),
                evidence_refs=[
                    f"chapters={','.join(str(ch) for ch in unresolved_review_chapters)}"
                ],
                suggested_fix="先处理这些章节的 review，再放行 band checkpoint。",
            )
        )
    if review_fail_chapters:
        issues.append(
            ContinuityIssue(
                rule_name="intra_band_consistency_review_fail",
                severity="error",
                description="band 内存在 latest review 为 fail 的章节。",
                reviewer=reviewer,
                issue_type="intra_band_consistency",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="intra_band_consistency"),
                evidence_refs=[
                    f"chapters={','.join(str(ch) for ch in review_fail_chapters)}"
                ],
                suggested_fix="先修复失败章节，或通过明确人工决策处理。",
            )
        )
    if pending_checkpoint_count:
        issues.append(
            ContinuityIssue(
                rule_name="intra_band_consistency_pending_checkpoint",
                severity="warning",
                description="同 band 存在未处理 checkpoint。",
                reviewer=reviewer,
                issue_type="intra_band_consistency",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="intra_band_consistency"),
                evidence_refs=[f"pending_checkpoint_count={pending_checkpoint_count}"],
                suggested_fix="处理已有 checkpoint 后再创建新的 band 放行判断。",
            )
        )
    return issues


def evaluate_next_band_task_compatibility(
    *,
    next_band_summary: NextBandSummary | None,
    combined_text: str,
    reviewer: str,
    target_scope: str,
) -> list[ContinuityIssue]:
    if next_band_summary is None:
        return []
    text = combined_text.strip()
    if not text:
        return []
    issues: list[ContinuityIssue] = []
    for task in next_band_summary.band_task_contract:
        evidence: list[str] = []
        forbidden_hits = [
            str(keyword)
            for keyword in task.forbidden_keywords
            if str(keyword or "").strip() and str(keyword) in text
        ]
        if forbidden_hits:
            evidence.append(f"forbidden_keywords={','.join(forbidden_hits)}")
        target = str(task.target_name or "").strip()
        if target and target in text:
            if task.task_type == "withhold" and any(
                keyword in text for keyword in _REVEAL_KEYWORDS
            ):
                evidence.append(f"withhold_target_revealed={target}")
            if any(
                keyword in text
                for keyword in (*_RESOURCE_CLOSURE_KEYWORDS, *_THREAD_CLOSURE_KEYWORDS)
            ):
                evidence.append(f"target_prematurely_closed={target}")
        if not evidence:
            continue
        issues.append(
            ContinuityIssue(
                rule_name="next_band_task_compatibility_risk",
                severity="warning",
                description=f"当前 band 末状态可能影响下一 band 任务：{task.description or task.target_name or task.task_type}",
                entity_names=[target] if target else [],
                reviewer=reviewer,
                issue_type="next_band_compatibility",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="next_band_compatibility"),
                evidence_refs=[
                    f"next_band_id={next_band_summary.band_id}",
                    f"task_type={task.task_type}",
                    *evidence,
                ],
                suggested_fix="保留下个 band 需要使用的状态，不要提前揭露、关闭或写死。",
            )
        )
    return issues


def evaluate_director_imbalance(
    *,
    review_metas: list[dict],
    band_stall_guard: int,
    reviewer: str,
    target_scope: str,
) -> list[ContinuityIssue]:
    issues: list[ContinuityIssue] = []
    if not review_metas:
        return issues
    ordered = sorted(
        review_metas,
        key=lambda item: int(item.get("chapter_number", 0) or 0),
    )
    experience_issue_types = {
        "payoff_miss",
        "stall",
        "hook_failure",
        "immersion",
        "experience",
        "director_imbalance",
    }
    has_experience_evidence = any(
        bool(meta.get("delivered_reward_tags"))
        or bool(
            experience_issue_types
            & {str(item) for item in (meta.get("issue_types") or [])}
        )
        or any(
            str(ref).startswith(("scene:", "draft_event:", "thread:", "state:"))
            for ref in (meta.get("evidence_refs") or [])
        )
        for meta in ordered
    )
    if not has_experience_evidence:
        return []
    empty_delivery_streak: list[int] = []
    reward_chapters: list[int] = []
    planned_mystery = 0
    delivered_mystery = 0
    planned_social_or_emotion: list[int] = []
    delivered_social_or_emotion: set[int] = set()
    setup_like_count = 0
    delivery_count = 0
    for meta in ordered:
        chapter_number = int(meta.get("chapter_number", 0) or 0)
        planned = {
            str(item)
            for item in (meta.get("planned_reward_tags") or [])
            if str(item).strip()
        }
        delivered = {
            str(item)
            for item in (meta.get("delivered_reward_tags") or [])
            if str(item).strip()
        }
        issue_types = {
            str(item) for item in (meta.get("issue_types") or []) if str(item).strip()
        }
        notes = " ".join(str(item) for item in (meta.get("review_notes") or []))
        if delivered:
            reward_chapters.append(chapter_number)
            delivery_count += 1
            empty_delivery_streak = []
        elif planned:
            empty_delivery_streak.append(chapter_number)
        if len(empty_delivery_streak) >= 2:
            issues.append(
                ContinuityIssue(
                    rule_name="director_payoff_consecutive_missing",
                    severity="warning",
                    description="band 内连续章节计划了 reward 但实际回报缺失。",
                    reviewer=reviewer,
                    issue_type="director_imbalance",
                    target_scope=target_scope,
                    issue_group=issue_group_for_issue(issue_type="director_imbalance"),
                    evidence_refs=[
                        f"chapters={','.join(str(ch) for ch in empty_delivery_streak[-2:])}"
                    ],
                    suggested_fix="下一章或 checkpoint 前补足可感知 payoff，或重排 band reward。",
                )
            )
            empty_delivery_streak = empty_delivery_streak[-1:]
        if "mystery" in planned:
            planned_mystery += 1
        if "mystery" in delivered:
            delivered_mystery += 1
        if planned & {"social", "emotion"}:
            planned_social_or_emotion.append(chapter_number)
        if delivered & {"social", "emotion"}:
            delivered_social_or_emotion.add(chapter_number)
        if "setup" in notes or "setup" in issue_types or "伏笔" in notes:
            setup_like_count += 1

    if len(reward_chapters) >= 2:
        max_gap = max(
            later - earlier
            for earlier, later in zip(reward_chapters, reward_chapters[1:])
        )
        if band_stall_guard > 0 and max_gap > band_stall_guard:
            issues.append(
                ContinuityIssue(
                    rule_name="director_reward_gap_exceeded",
                    severity="warning",
                    description="band 内实际 reward 间隔超过 stall guard。",
                    reviewer=reviewer,
                    issue_type="director_imbalance",
                    target_scope=target_scope,
                    issue_group=issue_group_for_issue(issue_type="director_imbalance"),
                    evidence_refs=[
                        f"reward_chapters={','.join(str(ch) for ch in reward_chapters)}",
                        f"stall_guard={band_stall_guard}",
                    ],
                    suggested_fix="缩短 reward gap，或把计划回报提前到当前 band。",
                )
            )
    elif (
        band_stall_guard > 0 and len(ordered) > band_stall_guard and not reward_chapters
    ):
        issues.append(
            ContinuityIssue(
                rule_name="director_reward_gap_exceeded",
                severity="warning",
                description="band 内尚无实际 reward 交付，超过 stall guard 容忍。",
                reviewer=reviewer,
                issue_type="director_imbalance",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="director_imbalance"),
                evidence_refs=[
                    f"chapters={len(ordered)}",
                    f"stall_guard={band_stall_guard}",
                ],
                suggested_fix="补一个明确的 power/social/mystery/emotion/justice 回报。",
            )
        )
    if setup_like_count >= 2 and delivery_count == 0:
        issues.append(
            ContinuityIssue(
                rule_name="director_setup_without_delivery",
                severity="warning",
                description="band 内 setup/伏笔偏多，但缺少对应 delivery。",
                reviewer=reviewer,
                issue_type="director_imbalance",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="director_imbalance"),
                evidence_refs=[
                    f"setup_like_count={setup_like_count}",
                    "delivery_count=0",
                ],
                suggested_fix="减少继续铺垫，补一个可验证交付点。",
            )
        )
    unresolved_relation = [
        chapter
        for chapter in planned_social_or_emotion
        if chapter not in delivered_social_or_emotion
    ]
    if len(unresolved_relation) >= 2:
        issues.append(
            ContinuityIssue(
                rule_name="director_relationship_shift_unresolved",
                severity="warning",
                description="关系/情绪/地位变化计划多次出现，但实际交付不足。",
                reviewer=reviewer,
                issue_type="director_imbalance",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="director_imbalance"),
                evidence_refs=[
                    f"chapters={','.join(str(ch) for ch in unresolved_relation[:4])}"
                ],
                suggested_fix="把关系、情绪或地位变化写成可见场面，而不是继续延后。",
            )
        )
    if planned_mystery >= 2 and delivered_mystery == 0:
        issues.append(
            ContinuityIssue(
                rule_name="director_mystery_without_clarification",
                severity="warning",
                description="mystery 计划连续堆叠，但缺少 clarification 或半揭晓。",
                reviewer=reviewer,
                issue_type="director_imbalance",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="director_imbalance"),
                evidence_refs=[
                    f"planned_mystery={planned_mystery}",
                    "delivered_mystery=0",
                ],
                suggested_fix="安排一个可理解的线索兑现或规则澄清，避免只堆悬念。",
            )
        )
    return issues


def _task_is_satisfied(task: PlanTaskItem, combined_text: str) -> bool:
    text = combined_text.strip()
    if not text:
        return False
    required_hits = [
        keyword
        for keyword in task.required_keywords
        if str(keyword or "").strip() and str(keyword) in text
    ]
    forbidden_hits = [
        keyword
        for keyword in task.forbidden_keywords
        if str(keyword or "").strip() and str(keyword) in text
    ]
    target_hit = bool(task.target_name and task.target_name in text)
    description_hit = bool(task.description and task.description in text)
    if task.task_type == "withhold":
        if forbidden_hits:
            return False
        if task.required_keywords:
            return len(required_hits) == len(task.required_keywords)
        return not description_hit
    if task.required_keywords:
        return len(required_hits) == len(task.required_keywords)
    if task.target_name:
        return target_hit
    # A prose description is guidance, not a machine-checkable assertion. Exact
    # sentence matching would report ordinary paraphrases as missing delivery.
    return True


def _constraint_severity(level: str) -> str:
    return {
        "hard": "error",
        "soft": "warning",
        "hint": "info",
    }.get(str(level or "soft"), "warning")


def _constraint_triggered(
    constraint: NarrativeConstraintInfo,
    *,
    combined_text: str,
    lower_text: str,
    state_changes: list[StateChangeCandidate],
    events: list[EventCandidate],
    thread_beats: list[ThreadBeatCandidate],
) -> tuple[bool, str]:
    subject = str(constraint.subject_name or "").strip()
    payload = dict(constraint.payload or {})
    payload_keywords = [
        str(item).strip()
        for item in (payload.get("keywords") or payload.get("forbidden_keywords") or [])
        if str(item).strip()
    ]
    if constraint.constraint_type == "character_availability":
        for change in state_changes:
            if subject and change.entity_name != subject:
                continue
            new_value = str(change.new_value or "")
            if any(keyword in new_value for keyword in _DEATH_KEYWORDS):
                return (
                    True,
                    f"state_change={change.entity_name}:{change.field}->{new_value}",
                )
        if (
            subject
            and subject in combined_text
            and text_has_unnegated_keyword(combined_text, _DEATH_KEYWORDS)
        ):
            return True, f"subject={subject}"
    elif constraint.constraint_type == "secret_withhold":
        if payload_keywords:
            keyword = first_unnegated_keyword(combined_text, tuple(payload_keywords))
            if keyword:
                return True, f"keyword={keyword}"
        if (
            subject
            and subject in combined_text
            and text_has_unnegated_keyword(combined_text, _REVEAL_KEYWORDS)
        ):
            return True, f"subject={subject}"
    elif constraint.constraint_type == "relationship_preserve":
        if (
            subject
            and subject in combined_text
            and text_has_unnegated_keyword(combined_text, _RELATION_BREAK_KEYWORDS)
        ):
            return True, f"subject={subject}"
    elif constraint.constraint_type == "thread_keep_open":
        for beat in thread_beats:
            if subject and beat.thread_name != subject:
                continue
            if beat.beat_type == "resolution":
                return True, f"thread={beat.thread_name}"
    elif constraint.constraint_type == "location_availability":
        for change in state_changes:
            if subject and change.entity_name != subject:
                continue
            if any(
                keyword in str(change.new_value or "")
                for keyword in _LOCATION_DESTROY_KEYWORDS
            ):
                return True, f"state_change={change.entity_name}:{change.field}"
        if (
            subject
            and subject in combined_text
            and text_has_unnegated_keyword(combined_text, _LOCATION_DESTROY_KEYWORDS)
        ):
            return True, f"subject={subject}"
    elif constraint.constraint_type == "rule_preserve":
        for change in state_changes:
            if subject and change.entity_name != subject:
                continue
            if any(
                keyword in str(change.new_value or "")
                for keyword in _RULE_BREAK_KEYWORDS
            ):
                return True, f"state_change={change.entity_name}:{change.field}"
    if payload_keywords:
        keyword = first_unnegated_keyword(combined_text, tuple(payload_keywords))
        if keyword:
            return True, f"keyword={keyword}"
    if subject and any(
        subject == name for event in events for name in event.involved_entity_names
    ):
        if constraint.constraint_type in {
            "character_availability",
            "secret_withhold",
        } and text_has_unnegated_keyword(
            combined_text,
            (*_DEATH_KEYWORDS, *_REVEAL_KEYWORDS),
        ):
            return True, f"event_subject={subject}"
    return False, ""


def _resource_closure_category(text: str) -> str:
    if any(keyword in text for keyword in _DEATH_KEYWORDS):
        return "character_locked_out"
    if any(keyword in text for keyword in _RELATION_BREAK_KEYWORDS):
        return "relationship_closed_too_early"
    if any(keyword in text for keyword in _REVEAL_KEYWORDS):
        return "secret_over_explained"
    if any(keyword in text for keyword in _GROWTH_COMPLETION_KEYWORDS):
        return "growth_arc_completed_too_early"
    if any(keyword in text for keyword in _THREAD_CLOSURE_KEYWORDS):
        return "thread_closed_too_early"
    return "thread_closed_too_early"


def _band_checkpoint_gate_outcome(
    *,
    project_id: str,
    checkpoint_id: str,
    band_id: str,
    chapter_number: int,
    status: str,
    issues: list[BandCheckpointIssueInfo],
    policy_version: int = 0,
) -> GateOutcome:
    normalized_status = str(status or "error")
    decision = {
        "pass": "pass",
        "warn": "warn",
        "pending": "warn",
        "fail": "block",
        "error": "error",
        "overridden": "approve",
    }.get(normalized_status, "error")
    issue_keys = list(
        dict.fromkeys(str(issue.code) for issue in issues if str(issue.code))
    )
    issue_groups = list(
        dict.fromkeys(
            str(issue.issue_group or "")
            or issue_group_for_issue(code=str(issue.code or ""))
            for issue in issues
            if str(issue.code or "")
        )
    )
    return GateOutcome(
        gate_id="band_checkpoint",
        responsibility_domain="band_integrity",
        scope="band",
        candidate_id=checkpoint_id,
        chapter_number=chapter_number,
        band_id=band_id,
        policy_version=policy_version,
        fired=normalized_status != "pass",
        decision=decision,
        blocked=normalized_status in {"fail", "error"},
        issue_keys=issue_keys,
        issue_groups=[group for group in issue_groups if group],
        evidence_refs=list(
            dict.fromkeys(str(issue.detail) for issue in issues if str(issue.detail))
        ),
    )


@dataclass(frozen=True)
class CheckpointEvidence:
    current: bool
    effective_status: str
    input_sha256: str = ""
    reason: str = ""


@dataclass(frozen=True)
class _CheckpointEvaluation:
    detail: BandCheckpointDetail
    inputs: dict[str, Any]
    book_revision: int

    @property
    def input_sha256(self):
        return _checkpoint_digest(self.inputs)

    @property
    def result_sha256(self):
        return _checkpoint_digest(self.detail.model_dump(mode="json"))


def _checkpoint_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class BandCheckpointEvaluator:
    """The existing deterministic checks, with immutable evaluation evidence."""

    def __init__(self, session: Session):
        self.session = session

    def _evaluate(
        self, project_id: str, chapter_number: int
    ) -> _CheckpointEvaluation | None:
        from forwin.state.repo import StateRepository

        session = self.session
        repo = StateRepository(session)
        project = session.scalar(
            select(Project)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ValueError("Checkpoint project is unavailable")
        active_arc = repo.get_active_arc_plan(project_id)
        band_row = None
        if active_arc is not None:
            band_row = (
                session.query(BandExperiencePlan)
                .filter(
                    BandExperiencePlan.project_id == project_id,
                    BandExperiencePlan.arc_id == active_arc.id,
                    BandExperiencePlan.chapter_start <= chapter_number,
                    BandExperiencePlan.chapter_end == chapter_number,
                )
                .order_by(
                    BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc()
                )
                .populate_existing()
                .first()
            )
        if band_row is None:
            band_row = repo.get_band_row_for_chapter(project_id, chapter_number)
        if band_row is None or int(band_row.chapter_end or 0) != chapter_number:
            return None
        band_plans = (
            session.query(ChapterPlan)
            .filter(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number >= int(band_row.chapter_start or 0),
                ChapterPlan.chapter_number <= int(band_row.chapter_end or 0),
            )
            .order_by(ChapterPlan.chapter_number.asc())
            .populate_existing()
            .all()
        )
        unresolved = [
            row
            for row in repo.list_band_checkpoints(project_id, band_id=band_row.band_id)
            if row.status == "pending"
        ]
        constraints_enabled = (
            bool(repo.future_constraints_enabled(project_id))
            if hasattr(repo, "future_constraints_enabled")
            else True
        )
        issues: list[BandCheckpointIssueInfo] = []
        status = "pass"
        chapter_bodies: list[str] = []
        chapter_summaries: list[str] = []
        unresolved_review_chapters: list[int] = []
        review_fail_chapters: list[int] = []
        review_metas: list[dict[str, Any]] = []
        drafts = load_latest_drafts_by_plan_id(
            session, [plan.id for plan in band_plans]
        )
        accepted_reviews = load_candidate_reviews_by_draft_id(
            session,
            [
                drafts[plan.id].id
                for plan in band_plans
                if plan.status == "accepted" and plan.id in drafts
            ],
        )
        chapter_inputs = []
        for plan in band_plans:
            if str(plan.status or "") == "needs_review":
                unresolved_review_chapters.append(int(plan.chapter_number or 0))
            latest_draft = drafts.get(plan.id)
            latest_review = None
            if latest_draft is not None:
                latest_review = (
                    accepted_reviews.get(latest_draft.id)
                    if plan.status == "accepted"
                    else session.query(ChapterReview)
                    .filter(ChapterReview.draft_id == latest_draft.id)
                    .order_by(ChapterReview.created_at.desc(), ChapterReview.id.desc())
                    .populate_existing()
                    .first()
                )
            chapter_input = self._chapter_input(plan, latest_draft, latest_review)
            chapter_inputs.append(chapter_input)
            if plan.status == "accepted" and (
                latest_draft is None
                or not str(latest_draft.body_text or "").strip()
                or latest_draft.id not in accepted_reviews
            ):
                issues.append(
                    BandCheckpointIssueInfo(
                        code="accepted_chapter_evidence_missing",
                        severity="error",
                        issue_group=issue_group_for_issue(
                            code="intra_band_consistency"
                        ),
                        description=f"第 {plan.chapter_number} 章缺少有效 Canon 正文或绑定评审。",
                        detail=f"chapter_plan_id={plan.id}",
                    )
                )
                continue
            if latest_draft is None:
                continue
            chapter_bodies.append(str(latest_draft.body_text or ""))
            chapter_summaries.append(str(latest_draft.summary or ""))
            if latest_review is not None and str(latest_review.verdict or "") == "fail":
                review_fail_chapters.append(int(plan.chapter_number or 0))
            if latest_review is not None:
                try:
                    review_meta = (
                        json.loads(latest_review.review_meta_json or "{}") or {}
                    )
                except (json.JSONDecodeError, TypeError):
                    review_meta = {}
                try:
                    review_issues = json.loads(latest_review.issues_json or "[]") or []
                except (json.JSONDecodeError, TypeError):
                    review_issues = []
                if isinstance(review_meta, dict):
                    review_meta["chapter_number"] = int(plan.chapter_number or 0)
                    review_meta["issue_types"] = [
                        str(item.get("issue_type") or item.get("rule_name") or "")
                        for item in review_issues
                        if isinstance(item, dict)
                    ]
                    review_metas.append(review_meta)
        if any(plan.status != "accepted" for plan in band_plans):
            status = "fail"
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_not_fully_accepted",
                    severity="error",
                    issue_group=issue_group_for_issue(code="intra_band_consistency"),
                    description="band 内仍有章节未 accepted。",
                )
            )
        if unresolved:
            status = "warn" if status == "pass" else status
            issues.append(
                BandCheckpointIssueInfo(
                    code="pending_checkpoint_exists",
                    severity="warning",
                    issue_group=issue_group_for_issue(code="intra_band_consistency"),
                    description="同 band 仍存在未处理 checkpoint。",
                )
            )
        intra_band_issues = evaluate_intra_band_consistency(
            unresolved_review_chapters=unresolved_review_chapters,
            review_fail_chapters=review_fail_chapters,
            pending_checkpoint_count=len(unresolved),
            reviewer="plan_control",
            target_scope="band",
        )
        for issue in intra_band_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="intra_band_consistency",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        combined_text = band_combined_text(
            chapter_bodies=chapter_bodies,
            chapter_summaries=chapter_summaries,
        )
        band_tasks = repo.get_band_task_contract_for_chapter(project_id, chapter_number)
        band_task_issues = evaluate_task_contract(
            band_tasks,
            combined_text=combined_text,
            reviewer="plan_control",
            issue_type="band_task_completion",
            target_scope="band",
        )
        for issue in band_task_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_task_completion",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        try:
            schedule_payload = json.loads(band_row.schedule_json or "{}")
        except (TypeError, json.JSONDecodeError):
            schedule_payload = {}
        band_schedule = (
            BandDelightSchedule.model_validate(schedule_payload)
            if isinstance(schedule_payload, dict)
            else None
        )
        obligation_repo = NarrativeObligationRepository(session)
        band_obligations = [
            *obligation_repo.list_active_for_context(
                project_id, chapter_number=chapter_number + 1
            ),
            *obligation_repo.list_planned_for_chapter(
                project_id, origin_chapter_number=chapter_number
            ),
        ]
        band_obligation_issues = evaluate_band_obligation_contract(
            band_schedule,
            obligations=band_obligations,
            band_end_chapter=chapter_number,
            reviewer="plan_control",
            target_scope="band",
        )
        for issue in band_obligation_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_obligation_completion",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        director_issues = evaluate_director_imbalance(
            review_metas=review_metas,
            band_stall_guard=int(getattr(band_row, "stall_guard_max_gap", 0) or 0),
            reviewer="plan_control",
            target_scope="band",
        )
        for issue in director_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="director_imbalance",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        next_band_summary = repo.get_next_band_summary(project_id, chapter_number)
        constraint_chapter = (
            int(next_band_summary.chapter_start or 0)
            if next_band_summary is not None
            else chapter_number + 1
        )
        future_constraints = (
            repo.list_active_narrative_constraints(
                project_id,
                chapter_number=max(chapter_number, constraint_chapter),
            )
            if constraints_enabled
            else []
        )
        if constraints_enabled:
            compatibility_issues = evaluate_constraint_issues(
                future_constraints,
                combined_text=combined_text,
                state_changes=[],
                events=[],
                thread_beats=[],
                reviewer="plan_control",
                issue_type="next_band_compatibility",
                target_scope="band",
            )
            compatibility_issues.extend(
                evaluate_next_band_task_compatibility(
                    next_band_summary=next_band_summary,
                    combined_text=combined_text,
                    reviewer="plan_control",
                    target_scope="band",
                )
            )
            for issue in compatibility_issues:
                issues.append(
                    BandCheckpointIssueInfo(
                        code="next_band_compatibility"
                        if issue.severity == "error"
                        else "future_constraint",
                        severity=issue.severity,
                        issue_group=issue.issue_group,
                        description=issue.description,
                        detail="; ".join(issue.evidence_refs),
                    )
                )
            next_band_targets = [
                *[
                    task.target_name
                    for task in (
                        next_band_summary.band_task_contract
                        if next_band_summary is not None
                        else []
                    )
                    if str(task.target_name or "").strip()
                ],
                *[
                    constraint.subject_name
                    for constraint in future_constraints
                    if str(constraint.subject_name or "").strip()
                ],
            ]
            future_risk_issues = evaluate_resource_closure_risk(
                combined_text=combined_text,
                next_band_targets=list(dict.fromkeys(next_band_targets)),
                reviewer="plan_control",
                target_scope="band",
            )
            for issue in future_risk_issues:
                category = ""
                for ref in issue.evidence_refs:
                    text = str(ref or "")
                    if text.startswith("category="):
                        category = text.split("=", 1)[1].strip()
                        break
                issues.append(
                    BandCheckpointIssueInfo(
                        code="future_resource_preservation",
                        severity="warning",
                        category=category,
                        issue_group=issue.issue_group,
                        description=issue.description,
                        detail="; ".join(issue.evidence_refs),
                    )
                )
        if status != "fail" and any(issue.severity == "error" for issue in issues):
            status = "fail"
        elif status == "pass" and any(issue.severity == "warning" for issue in issues):
            status = "warn"
        summary = (
            "band checkpoint 通过。"
            if status == "pass"
            else "band checkpoint 需要人工处理。"
        )
        inputs = {
            "evaluation_version": "band-checkpoint-v1",
            "project_id": project_id,
            "band": {
                "id": band_row.id,
                "arc_id": band_row.arc_id,
                "band_id": band_row.band_id,
                "chapter_start": band_row.chapter_start,
                "chapter_end": band_row.chapter_end,
                "task_contract": [task.model_dump(mode="json") for task in band_tasks],
                "schedule": band_schedule.model_dump(mode="json")
                if band_schedule
                else None,
                "stall_guard_max_gap": int(band_row.stall_guard_max_gap or 0),
            },
            "chapters": chapter_inputs,
            "pending_checkpoints": sorted(row.id for row in unresolved),
            "obligations": [item.model_dump(mode="json") for item in band_obligations],
            "next_band": next_band_summary.model_dump(mode="json")
            if next_band_summary
            else None,
            "future_constraints_enabled": constraints_enabled,
            "future_constraints": [
                item.model_dump(mode="json") for item in future_constraints
            ],
        }
        return _CheckpointEvaluation(
            detail=BandCheckpointDetail(
                project_id=project_id,
                arc_id=band_row.arc_id,
                band_id=band_row.band_id,
                chapter_start=int(band_row.chapter_start or 0),
                chapter_end=int(band_row.chapter_end or 0),
                trigger_source="auto_band_end",
                boundary_kind="band_end",
                boundary_chapter=chapter_number,
                status=status,
                summary=summary,
                issues=issues,
            ),
            inputs=inputs,
            book_revision=int(project.book_revision or 0),
        )

    def _chapter_input(
        self,
        plan: ChapterPlan,
        draft: ChapterDraft | None,
        review: ChapterReview | None,
    ) -> dict[str, Any]:
        candidate = (
            self.session.scalar(
                select(CandidateDraftRecord).where(
                    CandidateDraftRecord.candidate_draft_id == draft.id
                )
            )
            if draft
            else None
        )
        return {
            "chapter_plan_id": plan.id,
            "chapter_number": plan.chapter_number,
            "status": plan.status,
            "active_commit_id": plan.active_commit_id or "",
            "draft_id": draft.id if draft else "",
            "candidate_id": candidate.id if candidate else "",
            "candidate_plan_revision": candidate.plan_revision if candidate else "",
            "body_sha256": hashlib.sha256(
                str(draft.body_text or "").encode()
            ).hexdigest()
            if draft
            else "",
            "summary_sha256": hashlib.sha256(
                str(draft.summary or "").encode()
            ).hexdigest()
            if draft
            else "",
            "review": self._review_input(review),
        }

    @staticmethod
    def _review_input(review: ChapterReview | None) -> dict[str, Any] | None:
        if review is None:
            return None
        return {
            "id": review.id,
            "verdict": review.verdict,
            "payload_sha256": _checkpoint_digest(
                [review.review_meta_json, review.issues_json]
            ),
        }

    def _matches(
        self, checkpoint: BandCheckpoint, evaluation: _CheckpointEvaluation | None
    ) -> bool:
        if evaluation is None or (
            checkpoint.project_id != evaluation.detail.project_id
            or checkpoint.arc_id != evaluation.detail.arc_id
            or checkpoint.band_id != evaluation.detail.band_id
            or checkpoint.chapter_start != evaluation.detail.chapter_start
            or checkpoint.chapter_end != evaluation.detail.chapter_end
        ):
            return False
        events = list(
            self.session.scalars(
                select(DecisionEvent)
                .where(
                    DecisionEvent.project_id == checkpoint.project_id,
                    DecisionEvent.related_object_type == "band_checkpoint",
                    DecisionEvent.related_object_id == checkpoint.id,
                    DecisionEvent.event_type
                    == DecisionEventType.BAND_CHECKPOINT_CREATED,
                )
                .execution_options(populate_existing=True)
            )
        )
        if len(events) != 1:
            return False
        try:
            proof = json.loads(events[0].payload_json)["checkpoint_evaluation"]
            return (
                proof["version"] == "band-checkpoint-v1"
                and proof["input_sha256"] == evaluation.input_sha256
                and proof["result_sha256"] == evaluation.result_sha256
                and proof["input_identity"] == evaluation.inputs
            )
        except (TypeError, ValueError, KeyError):
            return False

    def inspect(self, checkpoint: BandCheckpoint | None) -> CheckpointEvidence:
        if checkpoint is None:
            return CheckpointEvidence(False, "pending", reason="checkpoint_missing")
        if checkpoint.trigger_source != "auto_band_end":
            return CheckpointEvidence(True, str(checkpoint.status or "pending"))
        try:
            evaluation = self._evaluate(
                checkpoint.project_id, checkpoint.boundary_chapter
            )
        except Exception:  # noqa: BLE001 - incomplete current inputs cannot authorize an old result
            return CheckpointEvidence(
                False, "pending", reason="checkpoint_inputs_unknown"
            )
        current = self._matches(checkpoint, evaluation)
        return CheckpointEvidence(
            current,
            normalize_checkpoint_status(checkpoint.status) if current else "pending",
            evaluation.input_sha256 if evaluation else "",
            "" if current else "checkpoint_evidence_stale",
        )

    def refresh(
        self,
        project_id: str,
        chapter_number: int,
        *,
        audit: PipelineAuditContext | None = None,
    ) -> BandCheckpoint | None:
        from forwin.canon.projection_lock import lock_projection_project
        from forwin.observability.pipeline_trace import (
            PipelineAuditContext,
            PipelineTraceRecorder,
        )
        from forwin.state.updater import StateUpdater

        lock_projection_project(self.session, project_id)
        evaluation = self._evaluate(project_id, chapter_number)
        if evaluation is None:
            return None
        existing = self.session.scalar(
            select(BandCheckpoint)
            .where(
                BandCheckpoint.project_id == project_id,
                BandCheckpoint.band_id == evaluation.detail.band_id,
                BandCheckpoint.trigger_source == "auto_band_end",
                BandCheckpoint.boundary_kind == "band_end",
                BandCheckpoint.boundary_chapter == chapter_number,
            )
            .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        if existing is not None and self._matches(existing, evaluation):
            return existing
        previous_created_at = self.session.scalar(
            select(func.max(BandCheckpoint.created_at)).where(
                BandCheckpoint.project_id == project_id
            )
        )
        created_at = datetime.now(UTC).replace(tzinfo=None)
        if previous_created_at is not None:
            created_at = max(
                created_at, previous_created_at + timedelta(microseconds=1)
            )
        updater = StateUpdater(self.session)
        row = updater.save_band_checkpoint(evaluation.detail)
        # PostgreSQL transaction timestamps can tie; UUID ordering is not evaluation order.
        row.created_at = created_at
        recorder = PipelineTraceRecorder(
            audit=audit or PipelineAuditContext(),
            artifact_store=None,
            observability=None,
        )
        recorder.record_event(
            updater=updater,
            project_id=project_id,
            band_id=row.band_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.BAND_CHECKPOINT_CREATED,
            scope="band",
            summary=row.summary,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
            payload=attach_gate_outcome(
                {
                    "status": row.status,
                    "chapter_review_form_result": {},
                    "band_checkpoint_mode": "chapter_review_form",
                    "checkpoint_evaluation": {
                        "version": "band-checkpoint-v1",
                        "input_sha256": evaluation.input_sha256,
                        "result_sha256": evaluation.result_sha256,
                        "input_identity": evaluation.inputs,
                        "evaluated_book_revision": evaluation.book_revision,
                    },
                },
                _band_checkpoint_gate_outcome(
                    project_id=project_id,
                    checkpoint_id=row.id,
                    band_id=row.band_id,
                    chapter_number=chapter_number,
                    status=row.status,
                    issues=evaluation.detail.issues,
                ),
            ),
        )
        return row
