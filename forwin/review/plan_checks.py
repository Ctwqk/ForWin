from __future__ import annotations

from forwin.book_state.extraction.chapter_delta_schema import (
    NEW_REF_PREFIX,
    ChapterDeltaExtraction,
)
from forwin.planning.constraints import NarrativeConstraintInfo
from forwin.review.issue_groups import issue_group_for_issue
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.experience import BandDelightSchedule
from forwin.protocol.review import ContinuityIssue


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
        or bool(meta.get("thread_beat_types"))
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
        thread_beat_types = {
            str(item) for item in (meta.get("thread_beat_types") or []) if str(item).strip()
        }
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
        if "setup" in thread_beat_types:
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


def evaluate_constraint_issues(
    constraints: list[NarrativeConstraintInfo],
    *,
    chapter_delta: ChapterDeltaExtraction | None,
    reviewer: str,
    issue_type: str,
    target_scope: str,
) -> list[ContinuityIssue]:
    issues: list[ContinuityIssue] = []
    for constraint in constraints:
        severity = _constraint_severity(constraint.level)
        entity_names = [constraint.subject_name] if constraint.subject_name else []
        evidence_refs = [
            f"constraint={constraint.id}",
            f"constraint_type={constraint.constraint_type}",
        ]
        matched, detail = _constraint_triggered(
            constraint,
            chapter_delta=chapter_delta,
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


def _constraint_severity(level: str) -> str:
    return {
        "hard": "error",
        "soft": "warning",
        "hint": "info",
    }.get(str(level or "soft"), "warning")


def _ref_label(ref: str) -> str:
    """A chapter delta ref as compared with constraint subjects: ``new:`` names lose the prefix."""
    return ref.strip().removeprefix(NEW_REF_PREFIX).strip()


def _constraint_triggered(
    constraint: NarrativeConstraintInfo,
    *,
    chapter_delta: ChapterDeltaExtraction | None,
) -> tuple[bool, str]:
    if constraint.constraint_type != "thread_keep_open" or chapter_delta is None:
        return False, ""
    subject = str(constraint.subject_name or "").strip()
    for beat in chapter_delta.thread_beats:
        thread_name = _ref_label(beat.thread_ref or beat.new_thread_title)
        if subject and thread_name != subject:
            continue
        if beat.beat_type == "resolution":
            return True, f"thread={thread_name}"
    return False, ""
