from __future__ import annotations

from typing import Iterable

from forwin.governance import NarrativeConstraintInfo, NextBandSummary, PlanTaskItem, issue_group_for_issue
from forwin.protocol.review import ContinuityIssue
from forwin.protocol.state_change import EventCandidate, StateChangeCandidate, ThreadBeatCandidate
from forwin.protocol.writer import WriterOutput

_DEATH_KEYWORDS = ("死", "死亡", "身亡", "阵亡", "牺牲", "杀死", "写死")
_REVEAL_KEYWORDS = ("真相", "秘密", "身份", "揭露", "公开", "坦白", "曝光")
_RELATION_BREAK_KEYWORDS = ("决裂", "断绝", "反目", "分手", "离婚", "背叛")
_LOCATION_DESTROY_KEYWORDS = ("毁灭", "坍塌", "封锁", "不可进入", "失守", "焚毁")
_RULE_BREAK_KEYWORDS = ("失效", "崩坏", "破除", "废除", "不可逆", "解除")
_RESOURCE_CLOSURE_KEYWORDS = (
    "彻底解决",
    "完全结束",
    "永远离开",
    "彻底公开",
    "永久失去",
    "不可逆",
)
_THREAD_CLOSURE_KEYWORDS = ("结案", "了结", "落幕", "终结", "收束", "完结")
_GROWTH_COMPLETION_KEYWORDS = ("完成成长", "彻底成熟", "终于成为", "再无成长空间", "终极形态", "圆满毕业")


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
                    issue_group=issue_group_for_issue(issue_type=issue_type, rule_name="plan_task_unfulfilled"),
                    evidence_refs=[
                        f"task_type={task.task_type}",
                        f"task_source={task.source}",
                    ],
                    suggested_fix="补足该章/该 band 需要交付的规划任务，或显式调整任务合同。",
                )
            )
    return issues


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
        evidence_refs = [f"constraint={constraint.id}", f"constraint_type={constraint.constraint_type}"]
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
                issue_group=issue_group_for_issue(issue_type=issue_type, rule_name="future_constraint_violation"),
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
                issue_group=issue_group_for_issue(issue_type="future_resource_preservation", rule_name="future_resource_preservation_risk"),
                evidence_refs=[f"target={name}", f"category={category}"],
                suggested_fix="避免把后续 band 仍可能要使用的角色、线索或关系写成彻底关闭。",
            )
        )
    return issues


def evaluate_intra_band_consistency(
    *,
    unresolved_review_chapters: list[int],
    review_fail_chapters: list[int],
    provisional_failed: bool,
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
                evidence_refs=[f"chapters={','.join(str(ch) for ch in unresolved_review_chapters)}"],
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
                evidence_refs=[f"chapters={','.join(str(ch) for ch in review_fail_chapters)}"],
                suggested_fix="先修复失败章节，或通过明确人工决策处理。",
            )
        )
    if provisional_failed:
        issues.append(
            ContinuityIssue(
                rule_name="intra_band_consistency_provisional_failed",
                severity="error",
                description="band 的 provisional gate 存在失败记录。",
                reviewer=reviewer,
                issue_type="intra_band_consistency",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="intra_band_consistency"),
                evidence_refs=["provisional_failed=true"],
                suggested_fix="先处理 provisional gate 失败原因，再放行 band。",
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
            if task.task_type == "withhold" and any(keyword in text for keyword in _REVEAL_KEYWORDS):
                evidence.append(f"withhold_target_revealed={target}")
            if any(keyword in text for keyword in (*_RESOURCE_CLOSURE_KEYWORDS, *_THREAD_CLOSURE_KEYWORDS)):
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
        or bool(experience_issue_types & {str(item) for item in (meta.get("issue_types") or [])})
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
        planned = {str(item) for item in (meta.get("planned_reward_tags") or []) if str(item).strip()}
        delivered = {str(item) for item in (meta.get("delivered_reward_tags") or []) if str(item).strip()}
        issue_types = {str(item) for item in (meta.get("issue_types") or []) if str(item).strip()}
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
                    evidence_refs=[f"chapters={','.join(str(ch) for ch in empty_delivery_streak[-2:])}"],
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
                    evidence_refs=[f"reward_chapters={','.join(str(ch) for ch in reward_chapters)}", f"stall_guard={band_stall_guard}"],
                    suggested_fix="缩短 reward gap，或把计划回报提前到当前 band。",
                )
            )
    elif band_stall_guard > 0 and len(ordered) > band_stall_guard and not reward_chapters:
        issues.append(
            ContinuityIssue(
                rule_name="director_reward_gap_exceeded",
                severity="warning",
                description="band 内尚无实际 reward 交付，超过 stall guard 容忍。",
                reviewer=reviewer,
                issue_type="director_imbalance",
                target_scope=target_scope,
                issue_group=issue_group_for_issue(issue_type="director_imbalance"),
                evidence_refs=[f"chapters={len(ordered)}", f"stall_guard={band_stall_guard}"],
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
                evidence_refs=[f"setup_like_count={setup_like_count}", "delivery_count=0"],
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
                evidence_refs=[f"chapters={','.join(str(ch) for ch in unresolved_relation[:4])}"],
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
                evidence_refs=[f"planned_mystery={planned_mystery}", "delivered_mystery=0"],
                suggested_fix="安排一个可理解的线索兑现或规则澄清，避免只堆悬念。",
            )
        )
    return issues


def _task_is_satisfied(task: PlanTaskItem, combined_text: str) -> bool:
    text = combined_text.strip()
    if not text:
        return False
    required_hits = [keyword for keyword in task.required_keywords if str(keyword or "").strip() and str(keyword) in text]
    forbidden_hits = [keyword for keyword in task.forbidden_keywords if str(keyword or "").strip() and str(keyword) in text]
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
    return target_hit or description_hit


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
                return True, f"state_change={change.entity_name}:{change.field}->{new_value}"
        if subject and subject in combined_text and any(keyword in combined_text for keyword in _DEATH_KEYWORDS):
            return True, f"subject={subject}"
    elif constraint.constraint_type == "secret_withhold":
        if payload_keywords and any(keyword in combined_text for keyword in payload_keywords):
            return True, f"keyword={next(keyword for keyword in payload_keywords if keyword in combined_text)}"
        if subject and subject in combined_text and any(keyword in combined_text for keyword in _REVEAL_KEYWORDS):
            return True, f"subject={subject}"
    elif constraint.constraint_type == "relationship_preserve":
        if subject and subject in combined_text and any(keyword in combined_text for keyword in _RELATION_BREAK_KEYWORDS):
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
            if any(keyword in str(change.new_value or "") for keyword in _LOCATION_DESTROY_KEYWORDS):
                return True, f"state_change={change.entity_name}:{change.field}"
        if subject and subject in combined_text and any(keyword in combined_text for keyword in _LOCATION_DESTROY_KEYWORDS):
            return True, f"subject={subject}"
    elif constraint.constraint_type == "rule_preserve":
        for change in state_changes:
            if subject and change.entity_name != subject:
                continue
            if any(keyword in str(change.new_value or "") for keyword in _RULE_BREAK_KEYWORDS):
                return True, f"state_change={change.entity_name}:{change.field}"
    if payload_keywords and any(keyword.lower() in lower_text for keyword in payload_keywords):
        return True, f"keyword={next(keyword for keyword in payload_keywords if keyword.lower() in lower_text)}"
    if subject and any(subject == name for event in events for name in event.involved_entity_names):
        if constraint.constraint_type in {"character_availability", "secret_withhold"} and any(
            keyword in combined_text for keyword in (*_DEATH_KEYWORDS, *_REVEAL_KEYWORDS)
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
