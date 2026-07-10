from __future__ import annotations

from collections import Counter
import re
from typing import Any

from forwin.protocol.context import ChapterContextPack, LintSignal, ReviewContextPack
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.writer.llm_client import LLMClient
from .llm_webnovel import LLMWebNovelReviewer
from .map_movement import MapMovementReviewer


_TAG_KEYWORDS = {
    "power": ("突破", "胜", "压制", "变强", "爆发", "底牌", "升级", "反杀", "翻盘", "压过"),
    "social": ("众人", "名声", "地位", "羞辱", "服软", "威望", "脸面", "认错", "臣服", "震慑"),
    "justice": ("报应", "清算", "讨回", "惩罚", "伸张", "偿还", "公道", "制裁", "审判"),
    "mystery": ("线索", "秘密", "真相", "异象", "谜", "疑点", "暗示", "伏笔", "揭晓", "不对劲"),
    "emotion": ("心口", "哽", "痛", "愧", "想起", "难过", "愤怒", "温热", "害怕", "不舍"),
}

_HOOK_MARKERS = ("？", "?", "忽然", "但", "然而", "下一刻", "没想到", "原来", "直到这时", "才发现")
_BRIDGE_KEYWORDS = ("问题", "线索", "进展", "状态", "关系", "规则", "锚点", "悬念", "真相", "危机")


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, round(value, 3)))


def _trim(text: str, limit: int = 64) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)] + "…"


class WebNovelExperienceReviewer:
    def __init__(
        self,
        *,
        enabled: bool = True,
        llm_client: LLMClient | None = None,
        llm_enabled: bool | None = None,
        include_map_movement: bool = True,
        map_movement_reviewer: MapMovementReviewer | None = None,
    ) -> None:
        self.enabled = enabled
        self.llm_client = llm_client
        self.llm_enabled = bool(llm_client) if llm_enabled is None else llm_enabled
        self.include_map_movement = include_map_movement
        self.map_movement_reviewer = map_movement_reviewer or MapMovementReviewer()
        self.llm_webnovel_reviewer = LLMWebNovelReviewer(
            llm_client=llm_client,
            heuristic_reviewer=self,
            enabled=self.llm_enabled,
        )

    def __getattr__(self, name: str):
        if name in {"_llm_payload", "_llm_review_messages", "_repair_llm_json"}:
            return getattr(self.llm_webnovel_reviewer, name)
        raise AttributeError(name)

    def review(
        self,
        context: ReviewContextPack | ChapterContextPack,
        writer_output: WriterOutput,
        *,
        reviewer_skill_layers: list[object] | None = None,
    ) -> ReviewVerdict:
        review_context = self._normalize_context(context)
        if not self.enabled:
            return ReviewVerdict(verdict="pass", issues=[])
        if review_context.chapter_experience_plan is None:
            movement_issue = (
                self.map_movement_reviewer.first_issue(review_context, writer_output)
                if self.include_map_movement
                else None
            )
            if movement_issue is not None:
                return ReviewVerdict(
                    verdict="fail" if movement_issue.severity == "error" else "warn",
                    issues=[movement_issue],
                    recommended_action="rewrite" if movement_issue.severity == "error" else "pause_for_review",
                    reviewer_mode="heuristic_fallback",
                    evidence_refs=list(movement_issue.evidence_refs),
                )
            return ReviewVerdict(verdict="pass", issues=[])

        if self.llm_enabled and self.llm_client is not None:
            llm_verdict = self.llm_webnovel_reviewer.review(
                review_context,
                writer_output,
                reviewer_skill_layers=reviewer_skill_layers,
            )
            if llm_verdict is not None:
                return llm_verdict
            heuristic = self._review_with_heuristics(review_context, writer_output)
            prompt_trace = self.llm_webnovel_reviewer.fallback_trace(
                context=review_context,
                writer_output=writer_output,
                heuristic=heuristic,
            )
            if prompt_trace is not None:
                heuristic = heuristic.model_copy(
                    update={
                        "prompt_trace": prompt_trace,
                    }
                )
            return heuristic
        return self._review_with_heuristics(review_context, writer_output)

    def _normalize_context(
        self,
        context: ReviewContextPack | ChapterContextPack,
    ) -> ReviewContextPack:
        if isinstance(context, ReviewContextPack):
            return context
        return ReviewContextPack(
            project_id=context.project_id,
            project_title=context.project_title,
            chapter_number=context.chapter_number,
            chapter_plan_title=context.chapter_plan_title,
            chapter_plan_one_line=context.chapter_plan_one_line,
            chapter_goals=list(context.chapter_goals),
            previous_chapter_summaries=list(context.previous_chapter_summaries),
            genesis_context_refs=dict(context.genesis_context_refs),
            genesis_world_overview=context.genesis_world_overview,
            genesis_map_overview=context.genesis_map_overview,
            genesis_story_engine_summary=context.genesis_story_engine_summary,
            active_entities=list(context.active_entities),
            active_rules=[],
            active_threads=list(context.active_threads),
            timeline=context.timeline,
            world_pressure=context.world_pressure,
            reader_feedback=context.reader_feedback,
            audience_hints=context.audience_hints,
            reader_promise=context.reader_promise,
            arc_payoff_map=context.arc_payoff_map,
            band_delight_schedule=context.band_delight_schedule,
            band_task_contract=list(context.band_task_contract),
            chapter_experience_plan=context.chapter_experience_plan,
            chapter_task_contract=list(context.chapter_task_contract),
            active_future_constraints=list(context.active_future_constraints),
            next_band_summary=context.next_band_summary,
            map_context=dict(context.map_context),
            recent_canon_events=[],
            recent_rule_events=[],
            recent_review_notes=[],
            lint_signals=[],
            active_personality_contexts=list(context.active_personality_contexts),
        )

    @staticmethod
    def _confirmed_signal_refs(context: ReviewContextPack) -> list[str]:
        feedback = context.reader_feedback
        if feedback is None:
            return []
        refs: list[str] = []
        for signal in feedback.confirmed_signals:
            key = str(signal.signal_key or "").strip()
            if not key:
                continue
            refs.append(f"audience_signal:{key}")
        return refs

    def _review_with_heuristics(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> ReviewVerdict:
        plan = context.chapter_experience_plan
        schedule = context.band_delight_schedule
        planned_tags = list(plan.planned_reward_tags if plan is not None else [])
        delivered_tags, delivery_refs = self._collect_delivered_reward_evidence(writer_output)
        progress_score, progress_refs = self._progress_score(context, writer_output)
        immersion_score, immersion_refs = self._immersion_score(context, writer_output)
        hook_score, hook_refs = self._hook_score(context, writer_output)
        emotional_score, emotional_refs = self._emotional_score(context, writer_output, delivered_tags)
        understanding_score, understanding_refs = self._understanding_score(context, writer_output)
        reward_score = self._reward_score(planned_tags, delivered_tags, assessable=self._is_assessable(writer_output))
        stall_score, stall_refs = self._stall_tolerance_score(
            context=context,
            delivered_tags=delivered_tags,
            progress_score=progress_score,
        )
        issues: list[ContinuityIssue] = []
        verdict = "pass"

        cadence_issue = self._cadence_issue(
            context=context,
            delivered_tags=delivered_tags,
            progress_score=progress_score,
            stall_score=stall_score,
        )
        if cadence_issue is not None:
            verdict = "fail"
            issues.append(cadence_issue)

        scheduled_reward_tags = self._scheduled_reward_tags_for_chapter(context)
        has_progress_bridge = progress_score >= 0.34 or stall_score >= 0.45
        if scheduled_reward_tags and self._is_assessable(writer_output) and not delivered_tags:
            reward_contract_present = self._has_reward_delivery_contract(writer_output)
            scheduled_reward_severity = (
                "error"
                if reward_contract_present and not has_progress_bridge
                else "warning"
            )
            verdict = "fail" if scheduled_reward_severity == "error" else ("warn" if verdict == "pass" else verdict)
            issues.append(
                ContinuityIssue(
                    rule_name="scheduled_reward_missing",
                    severity=scheduled_reward_severity,
                    description=(
                        "当前章节被计划为回报章，但正文产物没有兑现出可锚定的 reward beat。"
                        if scheduled_reward_severity == "error"
                        else "当前章节被计划为回报章，但输出缺少可锚定 reward beat；因仍有微进展/问题支点或旧式输出缺少 scene reward 元数据，降级为警告。"
                    ),
                    reviewer="webnovel_experience",
                    issue_type="payoff_miss",
                    target_scope="scene",
                    evidence_refs=[
                        f"scheduled_reward_tags={scheduled_reward_tags}",
                        f"reward_contract_present={reward_contract_present}",
                        f"progress_score={progress_score}",
                        f"stall_score={stall_score}",
                        *delivery_refs[:3],
                    ],
                    suggested_fix="让至少一个计划 reward 通过事件、状态变化、公开场面或章末钩子落地。",
                )
            )
        elif planned_tags and self._is_assessable(writer_output) and reward_score < 0.5:
            verdict = "warn" if verdict == "pass" else verdict
            issues.append(
                ContinuityIssue(
                    rule_name="reward_delivery_thin",
                    severity="warning",
                    description="本章计划奖励与实际回报对齐不足，读者会感觉回报偏薄。",
                    reviewer="webnovel_experience",
                    issue_type="payoff_miss",
                    target_scope="scene",
                    evidence_refs=[
                        f"planned_reward_tags={planned_tags}",
                        f"delivered_reward_tags={delivered_tags}",
                        *delivery_refs[:3],
                    ],
                    suggested_fix="把至少一个 planned reward 移到可感知场面或章末结果上。",
                )
            )

        rule_issue = self._rule_consistency_issue(context, writer_output)
        if rule_issue is not None:
            verdict = "fail"
            issues.append(rule_issue)

        movement_issue = (
            self.map_movement_reviewer.first_issue(context, writer_output)
            if self.include_map_movement
            else None
        )
        if movement_issue is not None:
            verdict = "fail" if movement_issue.severity == "error" else ("warn" if verdict == "pass" else verdict)
            issues.append(movement_issue)

        if immersion_score < 0.4 and writer_output.scene_outputs:
            verdict = "fail"
            issues.append(
                ContinuityIssue(
                    rule_name="immersion_anchor_missing",
                    severity="error",
                    description="scene 层面的沉浸锚点过弱，读者不容易进入现场。",
                    reviewer="webnovel_experience",
                    issue_type="immersion",
                    target_scope="scene",
                    evidence_refs=immersion_refs[:5],
                    suggested_fix="补出时间/地点/感官/即时反应中的至少两类锚点。",
                )
            )

        if hook_score < 0.4:
            verdict = "warn" if verdict == "pass" else verdict
            issues.append(
                ContinuityIssue(
                    rule_name="hook_soft",
                    severity="warning",
                    description="章末继续意图偏弱，问题或危险没有被有效悬起。",
                    reviewer="webnovel_experience",
                    issue_type="hook_failure",
                    target_scope="scene",
                    evidence_refs=hook_refs[:3],
                    suggested_fix="在最后一段悬置一个更好的问题、代价或即时危险。",
                )
            )

        lint_issue = self._adopt_lint_signal(context.lint_signals)
        if lint_issue is not None:
            verdict = "warn" if verdict == "pass" else verdict
            issues.append(lint_issue)

        review_notes = [
            self._planned_vs_delivered_note(planned_tags, delivered_tags),
            (
                "当前章仍有继续阅读理由：问题梯子/微进展/规则锚点至少保持了一项。"
                if stall_score >= 0.45
                else "当前章缺少足够的继续阅读支点，需补问题梯子或可验证推进。"
            ),
        ]
        evidence_refs = list(
            dict.fromkeys(
                [
                    *delivery_refs,
                    *progress_refs,
                    *immersion_refs,
                    *hook_refs,
                    *understanding_refs,
                    *emotional_refs,
                    *stall_refs,
                    *[ref for issue in issues for ref in issue.evidence_refs],
                ]
            )
        )
        repair_instruction = None
        if verdict == "fail":
            repair_instruction = self._build_repair_instruction(
                context=context,
                issues=issues,
                planned_tags=planned_tags,
                delivered_tags=delivered_tags,
                evidence_refs=evidence_refs,
            )
        verdict_model = ReviewVerdict(
            verdict=verdict,
            issues=issues,
            recommended_action=(
                "rewrite" if verdict == "fail" else "pause_for_review" if verdict == "warn" else "continue"
            ),
            review_summary=(
                f"计划={','.join(planned_tags) or '无'} | 实际={','.join(delivered_tags) or '无'} | "
                f"presence={immersion_score} | stall={stall_score} | hook={hook_score}"
            ),
            planned_reward_tags=planned_tags,
            delivered_reward_tags=delivered_tags,
            experience_scores={
                "narrative_understanding": understanding_score,
                "attentional_focus": max(hook_score, stall_score),
                "emotional_engagement": emotional_score,
                "narrative_presence": immersion_score,
                "payoff_delivery": reward_score,
                "stall_tolerance": stall_score,
                "hook_efficiency": hook_score,
            },
            review_notes=review_notes,
            lint_signals=list(context.lint_signals),
            evidence_refs=evidence_refs[:12],
            confirmed_signal_refs=self._confirmed_signal_refs(context),
            reviewer_mode="heuristic_fallback",
            repair_instruction=repair_instruction,
        )
        return self.llm_webnovel_reviewer.anchor_verdict_evidence(verdict_model, context, writer_output)

    def _is_assessable(self, writer_output: WriterOutput) -> bool:
        return bool(
            writer_output.scene_outputs
            or writer_output.new_events
            or writer_output.thread_beats
            or writer_output.state_changes
            or writer_output.time_advance is not None
        )

    def _has_reward_delivery_contract(self, writer_output: WriterOutput) -> bool:
        return bool(writer_output.scene_outputs)

    def _collect_delivered_reward_evidence(
        self,
        writer_output: WriterOutput,
    ) -> tuple[list[str], list[str]]:
        delivered_tags: list[str] = []
        evidence_refs: list[str] = []
        for scene in writer_output.scene_outputs:
            if scene.reward_beat_tag:
                delivered_tags.append(scene.reward_beat_tag)
                evidence_refs.append(
                    f"scene:{scene.scene_no}:{scene.reward_beat_tag}:{_trim(scene.micro_summary or scene.scene_objective)}"
                )
        for beat in writer_output.thread_beats:
            inferred = self._infer_tags_from_text(f"{beat.thread_name} {beat.description}")
            for tag in inferred:
                delivered_tags.append(tag)
                evidence_refs.append(f"thread:{beat.thread_name}:{beat.beat_type}:{_trim(beat.description)}")
        for event in writer_output.new_events:
            inferred = self._infer_tags_from_text(event.summary)
            for tag in inferred:
                delivered_tags.append(tag)
                evidence_refs.append(f"event:{event.significance}:{_trim(event.summary)}")
        for change in writer_output.state_changes:
            inferred = self._infer_tags_from_text(
                f"{change.entity_name} {change.field} {change.new_value} {change.reason}"
            )
            for tag in inferred:
                delivered_tags.append(tag)
                evidence_refs.append(
                    f"state:{change.entity_name}.{change.field}:{_trim(change.old_value)}->{_trim(change.new_value)}"
                )
        deduped_tags = list(dict.fromkeys(tag for tag in delivered_tags if tag))
        deduped_refs = list(dict.fromkeys(evidence_refs))
        return deduped_tags, deduped_refs

    def _progress_score(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> tuple[float, list[str]]:
        refs: list[str] = []
        points = 0.0
        if writer_output.new_events:
            points += 0.35
            refs.extend(f"event:{event.significance}:{_trim(event.summary)}" for event in writer_output.new_events[:3])
        if writer_output.thread_beats:
            points += 0.25
            refs.extend(
                f"thread:{beat.thread_name}:{beat.beat_type}:{_trim(beat.description)}"
                for beat in writer_output.thread_beats[:3]
            )
        if writer_output.state_changes:
            points += 0.2
            refs.extend(
                f"state:{item.entity_name}.{item.field}:{_trim(item.new_value)}"
                for item in writer_output.state_changes[:3]
            )
        if writer_output.time_advance is not None:
            points += 0.1
            refs.append(
                f"time:{_trim(writer_output.time_advance.duration_description)}->{_trim(writer_output.time_advance.new_time_label)}"
            )
        if context.chapter_experience_plan is not None and context.chapter_experience_plan.progress_markers:
            refs.extend(
                f"progress_marker:{_trim(item)}"
                for item in context.chapter_experience_plan.progress_markers[:3]
            )
        if not self._is_assessable(writer_output):
            return 0.5, ["progress_evidence=absent", *refs]
        return _clamp_score(points), refs

    def _immersion_score(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> tuple[float, list[str]]:
        if not writer_output.scene_outputs:
            refs = [
                f"rule_anchor:{_trim(item)}"
                for item in (context.chapter_experience_plan.rule_anchors if context.chapter_experience_plan else [])[:2]
            ]
            return 0.5, ["scene_outputs=absent", *refs]
        hits = 0.0
        refs: list[str] = []
        for scene in writer_output.scene_outputs:
            if scene.immersion_anchor.strip():
                hits += 1.0
            if scene.scene_time_point.strip():
                hits += 0.5
            if scene.scene_location_id.strip():
                hits += 0.5
            refs.extend(
                [
                    f"scene:{scene.scene_no}:time={_trim(scene.scene_time_point)}",
                    f"scene:{scene.scene_no}:location={_trim(scene.scene_location_id)}",
                    f"scene:{scene.scene_no}:anchor={_trim(scene.immersion_anchor)}",
                ]
            )
        if context.chapter_experience_plan is not None and context.chapter_experience_plan.rule_anchors:
            hits += 0.5
            refs.extend(
                f"rule_anchor:{_trim(item)}"
                for item in context.chapter_experience_plan.rule_anchors[:2]
            )
        score = hits / max(1.0, len(writer_output.scene_outputs) * 2.0)
        return _clamp_score(score), refs

    def _hook_score(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> tuple[float, list[str]]:
        tail_excerpt = _trim((writer_output.body or "")[-180:], 80)
        summary_excerpt = _trim(writer_output.end_of_chapter_summary, 60)
        hits = sum(1 for marker in _HOOK_MARKERS if marker in tail_excerpt or marker in summary_excerpt)
        if context.chapter_experience_plan is not None and context.chapter_experience_plan.question_hook:
            hits += 1
        score = 0.0 if not (tail_excerpt or summary_excerpt) else min(1.0, 0.35 + hits * 0.12)
        return _clamp_score(score), [f"tail:{tail_excerpt}", f"summary:{summary_excerpt}"]

    def _emotional_score(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        delivered_tags: list[str],
    ) -> tuple[float, list[str]]:
        refs: list[str] = []
        score = 0.2
        if "emotion" in delivered_tags:
            score += 0.35
            refs.append("delivered_tag:emotion")
        if "social" in delivered_tags:
            score += 0.2
            refs.append("delivered_tag:social")
        plan = context.chapter_experience_plan
        if plan is not None and plan.relationship_or_status_shift:
            score += 0.15
            refs.append(f"relationship_or_status_shift:{_trim(plan.relationship_or_status_shift)}")
        relation_changes = [
            item for item in writer_output.state_changes if item.field in {"trust", "affection", "status", "standing", "relation"}
        ]
        if relation_changes:
            score += 0.15
            refs.extend(
                f"state:{item.entity_name}.{item.field}:{_trim(item.new_value)}"
                for item in relation_changes[:2]
            )
        return _clamp_score(score), refs

    def _understanding_score(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> tuple[float, list[str]]:
        refs: list[str] = []
        score = 0.3
        if context.chapter_goals:
            score += 0.1
            refs.extend(f"goal:{_trim(goal)}" for goal in context.chapter_goals[:2])
        if writer_output.new_events:
            score += 0.2
            refs.extend(f"event:{_trim(item.summary)}" for item in writer_output.new_events[:2])
        if writer_output.thread_beats:
            score += 0.15
            refs.extend(f"thread:{item.thread_name}:{_trim(item.description)}" for item in writer_output.thread_beats[:2])
        if context.chapter_experience_plan is not None and context.chapter_experience_plan.question_resolution:
            score += 0.1
            refs.append(f"question_resolution:{_trim(context.chapter_experience_plan.question_resolution)}")
        if context.chapter_experience_plan is not None and context.chapter_experience_plan.rule_anchors:
            score += 0.1
            refs.append(f"rule_anchor:{_trim(context.chapter_experience_plan.rule_anchors[0])}")
        return _clamp_score(score), refs

    def _reward_score(self, planned_tags: list[str], delivered_tags: list[str], *, assessable: bool) -> float:
        if not planned_tags:
            return 1.0
        if not assessable:
            return 0.5
        overlap = len(set(planned_tags) & set(delivered_tags))
        return _clamp_score(overlap / max(1, len(set(planned_tags))))

    def _stall_tolerance_score(
        self,
        *,
        context: ReviewContextPack,
        delivered_tags: list[str],
        progress_score: float,
    ) -> tuple[float, list[str]]:
        refs: list[str] = []
        score = 0.0
        plan = context.chapter_experience_plan
        if delivered_tags:
            score += 0.4
            refs.append(f"delivered_reward_tags={delivered_tags}")
        if progress_score >= 0.34:
            score += 0.25
            refs.append(f"progress_score={progress_score}")
        if plan is not None and (plan.question_hook or plan.question_resolution):
            score += 0.15
            refs.append(f"question_hook={_trim(plan.question_hook)}")
        if plan is not None and plan.relationship_or_status_shift:
            score += 0.1
            refs.append(f"relationship_shift={_trim(plan.relationship_or_status_shift)}")
        if plan is not None and plan.rule_anchors:
            score += 0.1
            refs.append(f"rule_anchor={_trim(plan.rule_anchors[0])}")
        return _clamp_score(score), refs

    def _scheduled_reward_tags_for_chapter(self, context: ReviewContextPack) -> list[str]:
        schedule = context.band_delight_schedule
        if schedule is None:
            return []
        return [
            item.category
            for item in schedule.scheduled_rewards
            if item.chapter_hint == context.chapter_number
        ]

    def _cadence_issue(
        self,
        *,
        context: ReviewContextPack,
        delivered_tags: list[str],
        progress_score: float,
        stall_score: float,
    ) -> ContinuityIssue | None:
        schedule = context.band_delight_schedule
        if schedule is None:
            return None
        reward_chapters = sorted(
            {
                item.chapter_hint
                for item in schedule.scheduled_rewards
                if schedule.chapter_start <= item.chapter_hint <= schedule.chapter_end
            }
        )
        if not reward_chapters:
            return ContinuityIssue(
                rule_name="band_cadence_empty",
                severity="error",
                description="当前 band 没有任何计划回报章，near-term cadence 为空。",
                reviewer="webnovel_experience",
                issue_type="stall",
                target_scope="band",
                evidence_refs=[f"band_id={schedule.band_id}", "scheduled_rewards=[]"],
                suggested_fix="重新分配 band 回报组合，至少覆盖 power/social/mystery 基础 mix。",
            )
        categories = Counter(item.category for item in schedule.scheduled_rewards)
        missing = [item for item in ("power", "social", "mystery") if categories.get(item, 0) <= 0]
        if missing:
            return ContinuityIssue(
                rule_name="band_reward_mix_missing",
                severity="error",
                description="当前 band 缺失基础 reward mix，读者难以形成稳定回报预期。",
                reviewer="webnovel_experience",
                issue_type="mixed",
                target_scope="band",
                evidence_refs=[f"band_id={schedule.band_id}", f"category_counts={dict(categories)}"],
                suggested_fix="补齐 power/progress、social dominance、mystery clue/reveal 三类近端回报。",
            )
        streak = 0
        for note in context.recent_review_notes:
            if note.chapter_number < schedule.chapter_start or note.chapter_number > schedule.chapter_end:
                continue
            if note.delivered_reward_tags or self._review_note_has_bridge(note):
                break
            streak += 1
        current_has_bridge = bool(delivered_tags) or progress_score >= 0.34 or stall_score >= 0.45
        if streak >= max(1, schedule.stall_guard_max_gap) and not current_has_bridge:
            return ContinuityIssue(
                rule_name="no_payoff_stall",
                severity="error",
                description="当前 band 已连续多章没有 reward 交付，而且问题梯子/微进展/关系变化/规则稳态也没有维持住。",
                reviewer="webnovel_experience",
                issue_type="stall",
                target_scope="band",
                evidence_refs=[
                    f"band_id={schedule.band_id}",
                    f"historical_stall_streak={streak}",
                    f"current_delivered_reward_tags={delivered_tags}",
                    f"current_stall_score={stall_score}",
                ],
                suggested_fix="重排 band 计划，缩短 reward gap，并让当前章至少落一个 reward 或明确问题梯子。",
            )
        return None

    def _rule_consistency_issue(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> ContinuityIssue | None:
        plan = context.chapter_experience_plan
        if plan is None:
            return None
        if "mystery" not in plan.planned_reward_tags:
            return None
        if plan.rule_anchors:
            return None
        if context.active_rules or context.recent_rule_events:
            return ContinuityIssue(
                rule_name="rule_legibility_gap",
                severity="error",
                description="本章承载 mystery/reveal，但没有把相关规则锚点写清，容易形成作者强行感。",
                reviewer="webnovel_experience",
                issue_type="immersion",
                target_scope="scene",
                evidence_refs=[
                    *(item.evidence_id for item in context.recent_rule_events[:2]),
                    *(f"active_rule:{item.name}" for item in context.active_rules[:2]),
                ],
                suggested_fix="在 scene 或 summary 中补出规则边界、代价或可验证限制。",
            )
        return None

    def _adopt_lint_signal(self, lint_signals: list[LintSignal]) -> ContinuityIssue | None:
        if not lint_signals:
            return None
        severity_rank = {"error": 3, "warning": 2, "info": 1}
        signal = sorted(
            lint_signals,
            key=lambda item: (-severity_rank.get(item.severity, 1), item.line, item.column),
        )[0]
        if signal.severity == "info":
            return None
        return ContinuityIssue(
            rule_name=f"lint_signal:{signal.tool}:{signal.code}",
            severity="warning",
            description=f"辅助 lint 信号提示该章可能存在表达或清晰度毛刺：{signal.message}",
            reviewer="webnovel_experience",
            issue_type="lint",
            target_scope="chapter",
            evidence_refs=list(signal.evidence_refs) or [f"lint:{signal.tool}:{signal.code}"],
            suggested_fix="仅在不破坏既有节奏的前提下做轻量修整。",
        )

    def _review_note_has_bridge(self, note) -> bool:
        if note.delivered_reward_tags:
            return True
        text = " ".join([note.summary, *note.review_notes, *note.evidence_refs])
        return any(keyword in text for keyword in _BRIDGE_KEYWORDS)

    def _planned_vs_delivered_note(self, planned_tags: list[str], delivered_tags: list[str]) -> str:
        if not planned_tags and not delivered_tags:
            return "本章没有显式 reward 计划，也没有识别到明显 reward 交付。"
        if not planned_tags:
            return f"本章未预设 reward 标签，但实际识别到 {','.join(delivered_tags)}。"
        overlap = set(planned_tags) & set(delivered_tags)
        if overlap:
            return f"计划奖励 {','.join(planned_tags)} 中已兑现 {','.join(sorted(overlap))}。"
        return f"计划奖励 {','.join(planned_tags)} 尚未形成足够明确的交付。"

    def _build_repair_instruction(
        self,
        *,
        context: ReviewContextPack,
        issues: list[ContinuityIssue],
        planned_tags: list[str],
        delivered_tags: list[str],
        evidence_refs: list[str],
    ) -> RepairInstruction:
        scope_rank = {"draft": 1, "chapter_plan": 2, "band_plan": 3}
        error_scopes = [
            (
                "band_plan"
                if str(issue.target_scope or "") == "arc"
                else "chapter_plan"
                if str(issue.target_scope or "") == "band"
                else "draft"
            )
            for issue in issues
            if issue.severity == "error"
        ]
        scope = max(error_scopes, key=lambda item: scope_rank.get(item, 1), default="draft")
        issue_types = {issue.issue_type for issue in issues if issue.severity == "error"}
        failure_type = "mixed" if len(issue_types) != 1 else next(iter(issue_types))
        plan = context.chapter_experience_plan
        schedule = context.band_delight_schedule
        design_patch: dict[str, Any] = {
            "planned_reward_tags": list(planned_tags or (plan.planned_reward_tags if plan else [])),
            "selected_template_ids": list(plan.selected_template_ids if plan else []),
            "hook_type": plan.hook_type if plan else "",
            "question_hook": plan.question_hook if plan else "",
            "question_resolution": plan.question_resolution if plan else "",
            "immersion_anchors": list(plan.immersion_anchors if plan else []),
            "progress_markers": list(plan.progress_markers if plan else []),
            "rule_anchors": list(plan.rule_anchors if plan else []),
            "relationship_or_status_shift": plan.relationship_or_status_shift if plan else "",
            "minimum_progress_channels": list(plan.minimum_progress_channels if plan else []),
        }
        if schedule is not None:
            design_patch["scheduled_rewards"] = [item.model_dump(mode="json") for item in schedule.scheduled_rewards]
            design_patch["curiosity_beats"] = [item.model_dump(mode="json") for item in schedule.curiosity_beats]
            design_patch["ambiguity_payoffs"] = [item.model_dump(mode="json") for item in schedule.ambiguity_payoffs]
            design_patch["immersion_anchor_scene_goal"] = schedule.immersion_anchor_scene_goal
        if failure_type == "payoff_miss" and delivered_tags:
            design_patch["planned_reward_tags"] = list(dict.fromkeys([*planned_tags, *delivered_tags]))
        if failure_type == "immersion" and not design_patch.get("rule_anchors"):
            design_patch["rule_anchors"] = [
                "补清本章涉及的规则边界、代价或不可违背之处。",
            ]
        return RepairInstruction(
            repair_scope=scope,  # type: ignore[arg-type]
            failure_type=failure_type,  # type: ignore[arg-type]
            must_fix=[item.description for item in issues if item.severity == "error"],
            must_preserve=[
                context.chapter_plan_title,
                context.chapter_plan_one_line,
                *(context.chapter_goals[:2]),
            ],
            scope_reason=(
                "band-level cadence or structure issues need plan repair"
                if scope == "band"
                else "scene-level execution issues should be repaired locally first"
            ),
            design_patch=design_patch,
            evidence_refs=evidence_refs[:12],
        )

    def choose_repair_escalation(
        self,
        *,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        review: ReviewVerdict,
        repair_attempts: list[dict[str, object]] | None = None,
    ) -> RepairInstruction:
        heuristic = self._review_with_heuristics(context, writer_output)
        base_instruction = (
            review.repair_instruction
            or heuristic.repair_instruction
            or RepairInstruction(
                repair_scope="band",
                failure_type="mixed",
                must_fix=[item.description for item in review.issues if item.severity == "error"],
                must_preserve=[
                    context.chapter_plan_title,
                    context.chapter_plan_one_line,
                    *(context.chapter_goals[:2]),
                ],
                scope_reason="fallback escalation keeps repair inside current band",
                design_patch={},
                evidence_refs=list(review.evidence_refs),
            )
        )
        if self.llm_enabled and self.llm_client is not None:
            llm_decision = self.llm_webnovel_reviewer.choose_repair_escalation(
                context=context,
                writer_output=writer_output,
                review=review,
                repair_attempts=repair_attempts or [],
                base_instruction=base_instruction,
            )
            if llm_decision is not None:
                return llm_decision
        return base_instruction.model_copy(
            update={
                "repair_scope": "band",
                "scope_reason": (
                    base_instruction.scope_reason
                    or "LLM escalation unavailable; keep the third repair inside the current band."
                ),
            }
        )

    @staticmethod
    def _infer_tags_from_text(text: str) -> list[str]:
        normalized = str(text or "")
        matched: list[str] = []
        for tag, keywords in _TAG_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                matched.append(tag)
        return matched
