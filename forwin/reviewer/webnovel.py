from __future__ import annotations

from collections import Counter
import json
import re
from typing import Any

from forwin.protocol.context import ChapterContextPack, LintSignal, ReviewContextPack
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.book_state import CognitionOverlay, MapEdge, MapNode
from forwin.protocol.writer import WriterOutput
from forwin.book_state.cognition import CognitionView
from forwin.map.pathfinding import MapGraph
from forwin.skills import inject_skill_layers
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure
from forwin.utils import LLMJSONParseError, parse_llm_json
from forwin.llm.compat import call_chat_compat
from forwin.writer.llm_client import LLMClient


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


def _duration_to_travel_time_budget(text: str) -> float | None:
    normalized = str(text or "").strip()
    if not normalized:
        return None
    if "一炷香" in normalized:
        return 0.5
    if "片刻" in normalized or "须臾" in normalized:
        return 0.25
    if "半个时辰" in normalized:
        return 1.0
    if "时辰" in normalized:
        return _extract_duration_number(normalized, default=1.0) * 2.0
    if "小时" in normalized:
        return _extract_duration_number(normalized, default=1.0)
    if "半日" in normalized or "半天" in normalized:
        return 12.0
    if any(token in normalized for token in ("次日", "翌日", "第二天", "一天", "一日")):
        return 24.0
    if "天" in normalized or "日" in normalized:
        return _extract_duration_number(normalized, default=1.0) * 24.0
    return None


def _extract_duration_number(text: str, *, default: float) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1))
    chinese_numbers = {
        "一": 1.0,
        "二": 2.0,
        "两": 2.0,
        "三": 3.0,
        "四": 4.0,
        "五": 5.0,
        "六": 6.0,
        "七": 7.0,
        "八": 8.0,
        "九": 9.0,
        "十": 10.0,
    }
    for key, value in chinese_numbers.items():
        if key in text:
            return value
    return default


def _edge_from_path_id(graph: MapGraph, edge_id: str) -> MapEdge | None:
    base_edge_id = str(edge_id or "").removesuffix("__reverse")
    return graph.edges_by_id.get(str(edge_id or "")) or graph.edges_by_id.get(base_edge_id)


def _float_policy_value(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class WebNovelExperienceReviewer:
    def __init__(
        self,
        *,
        enabled: bool = True,
        llm_client: LLMClient | None = None,
        llm_enabled: bool | None = None,
    ) -> None:
        self.enabled = enabled
        self.llm_client = llm_client
        self.llm_enabled = bool(llm_client) if llm_enabled is None else llm_enabled

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
            movement_issue = self._map_movement_issue(review_context, writer_output)
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
            llm_verdict = self._review_with_llm(
                review_context,
                writer_output,
                reviewer_skill_layers=reviewer_skill_layers,
            )
            if llm_verdict is not None:
                return llm_verdict
            heuristic = self._review_with_heuristics(review_context, writer_output)
            attempts = self._drain_llm_attempts()
            if attempts:
                heuristic = heuristic.model_copy(
                    update={
                        "prompt_trace": self._build_prompt_trace(
                            context=review_context,
                            writer_output=writer_output,
                            messages=[],
                            attempts=attempts,
                            output_summary={
                                "status": "failed",
                                "fallback": "heuristic",
                                "verdict": heuristic.verdict,
                                "issue_count": len(heuristic.issues),
                            },
                        )
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

    def _review_with_llm(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        *,
        reviewer_skill_layers: list[object] | None = None,
    ) -> ReviewVerdict | None:
        payload = self._llm_payload(context, writer_output)
        evidence_ids = [item["evidence_id"] for item in payload["evidence_index"]]
        messages = self._llm_review_messages(
            payload=payload,
            evidence_ids=evidence_ids,
            reviewer_skill_layers=reviewer_skill_layers,
        )
        try:
            raw = call_chat_compat(
                self.llm_client,
                messages,
                temperature=0.1,
                max_tokens=3000,
                timeout_seconds=45,
                retry_on_timeout=True,
                task_family="reviewer",
                stage_key="chapter_review",
                output_schema={"type": "object"},
            )
        except Exception:
            return None

        allowed_evidence_ids = set(evidence_ids)
        for repair_attempt in range(3):
            try:
                parsed = parse_llm_json(raw, error_prefix="WNER")
                verdict = self._verdict_from_payload(
                    payload=parsed,
                    context=context,
                    writer_output=writer_output,
                    fallback_on_invalid=False,
                    allowed_evidence_ids=allowed_evidence_ids,
                )
                return verdict.model_copy(
                    update={
                        "prompt_trace": self._build_prompt_trace(
                            context=context,
                            writer_output=writer_output,
                            messages=messages,
                            attempts=self._drain_llm_attempts(),
                            output_summary={
                                "status": "succeeded",
                                "verdict": verdict.verdict,
                                "issue_count": len(verdict.issues),
                                "repair_attempts": repair_attempt,
                            },
                        )
                    }
                )
            except (LLMJSONParseError, ValueError) as exc:
                mark_latest_attempt_parse_failure(
                    self.llm_client,
                    parser_name="WNER",
                    stage_key="chapter_review" if repair_attempt == 0 else "chapter_review_json_repair",
                    schema_name="review_json",
                    raw_output=raw,
                    error=exc,
                )
                if repair_attempt >= 2:
                    return None
                repaired = self._repair_llm_json(
                    raw=raw,
                    error=str(exc),
                    evidence_ids=evidence_ids,
                )
                if repaired is None:
                    return None
                raw = repaired
            except Exception:
                return None

    def _drain_llm_attempts(self) -> list[dict[str, object]]:
        drain = getattr(self.llm_client, "drain_llm_attempt_events", None)
        if not callable(drain):
            return []
        attempts = drain()
        return attempts if isinstance(attempts, list) else []

    @staticmethod
    def _build_prompt_trace(
        *,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        messages: list[dict],
        attempts: list[dict[str, object]],
        output_summary: dict[str, object],
    ) -> dict[str, object]:
        prompt_layers = [
            {"role": str(item.get("role", "")).strip(), "content": str(item.get("content", ""))}
            for item in messages
            if isinstance(item, dict)
        ]
        effective_system_prompt = "\n\n".join(
            str(item.get("content", "")).strip()
            for item in prompt_layers
            if str(item.get("role", "")).strip() == "system"
        )
        return {
            "trace_scope": "reviewer",
            "stage_key": "chapter_review",
            "template_id": "reviewer:chapter_review",
            "template_version": "v1",
            "effective_system_prompt": effective_system_prompt,
            "prompt_layers": prompt_layers,
            "input_snapshot": {
                "project_id": context.project_id,
                "chapter_number": context.chapter_number,
                "body_char_count": int(getattr(writer_output, "char_count", 0) or 0),
            },
            "model_profile": {},
            "attempts": attempts,
            "output_summary": {
                "chapter_number": context.chapter_number,
                **output_summary,
            },
        }

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

        movement_issue = self._map_movement_issue(context, writer_output)
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
        return self._anchor_verdict_evidence(verdict_model, context, writer_output)

    def _llm_payload(self, context: ReviewContextPack, writer_output: WriterOutput) -> dict[str, Any]:
        evidence_index: list[dict[str, Any]] = []
        seen_evidence_ids: set[str] = set()

        def add_evidence(evidence_id: str, kind: str, summary: str) -> None:
            if not evidence_id or evidence_id in seen_evidence_ids:
                return
            seen_evidence_ids.add(evidence_id)
            evidence_index.append(
                {
                    "evidence_id": evidence_id,
                    "kind": kind,
                    "summary": _trim(summary, 120),
                }
            )

        if context.reader_promise is not None:
            add_evidence("overlay:reader_promise", "overlay", context.reader_promise.model_dump_json())
        if context.arc_payoff_map is not None:
            add_evidence("overlay:arc_payoff_map", "overlay", context.arc_payoff_map.model_dump_json())
        if context.band_delight_schedule is not None:
            add_evidence(
                "overlay:band_delight_schedule",
                "overlay",
                context.band_delight_schedule.model_dump_json(),
            )
        if context.chapter_experience_plan is not None:
            add_evidence(
                "overlay:chapter_experience_plan",
                "overlay",
                context.chapter_experience_plan.model_dump_json(),
            )
        if context.timeline is not None:
            add_evidence("world:timeline", "world", context.timeline.model_dump_json())
        if context.world_pressure is not None:
            add_evidence("world:pressure", "world", context.world_pressure.model_dump_json())
        if getattr(context, "world_context", None) is not None:
            world_context = context.world_context
            if world_context.snapshot_id:
                add_evidence(
                    f"world_model:snapshot:{world_context.snapshot_id}",
                    "world_model",
                    world_context.model_dump_json(),
                )
            for page in world_context.relevant_world_pages[:5]:
                add_evidence(f"world_model:page:{page.page_key}", "world_model_page", page.markdown[:500])
            for conflict in world_context.active_world_conflicts[:5]:
                add_evidence(
                    f"world_model:conflict:{conflict.id or conflict.subject_key}",
                    "world_model_conflict",
                    conflict.description,
                )
        for item in context.active_rules[:5]:
            add_evidence(f"active_rule:{item.entity_id or item.name}", "active_rule", item.description)

        add_evidence("draft:summary", "draft", writer_output.end_of_chapter_summary)
        add_evidence("draft:body_head", "draft", writer_output.body[:500])
        add_evidence("draft:body_tail", "draft", writer_output.body[-500:])
        for scene in writer_output.scene_outputs[:4]:
            add_evidence(
                f"scene:{scene.scene_no}",
                "scene",
                f"{scene.micro_summary or scene.scene_objective} | reward={scene.reward_beat_tag} | anchor={scene.immersion_anchor}",
            )
        for event in writer_output.new_events[:5]:
            add_evidence(
                f"draft_event:{event.significance}:{_trim(event.summary, 24)}",
                "draft_event",
                event.summary,
            )
        for beat in writer_output.thread_beats[:4]:
            add_evidence(
                f"thread:{beat.thread_name}:{beat.beat_type}",
                "thread",
                beat.description,
            )
        for change in writer_output.state_changes[:5]:
            add_evidence(
                f"state:{change.entity_name}:{change.field}",
                "state",
                f"{change.old_value}->{change.new_value} | {change.reason}",
            )
        if writer_output.time_advance is not None:
            add_evidence("time:advance", "time", writer_output.time_advance.model_dump_json())
        for item in context.recent_canon_events[:5]:
            add_evidence(item.evidence_id or f"canon_event:{item.event_id}", "canon_event", item.summary)
        for item in context.recent_rule_events[:5]:
            add_evidence(item.evidence_id or f"rule_event:{item.event_id}", "rule_event", item.summary)
        for index, item in enumerate(context.recent_review_notes[:5], start=1):
            add_evidence(f"review_note:{item.chapter_number}:{index}", "review_note", item.summary)
        for signal in context.lint_signals[:8]:
            add_evidence(
                f"lint:{signal.tool}:{signal.code}:{signal.line}:{signal.column}",
                "lint",
                signal.message,
            )
        if context.reader_feedback is not None:
            for signal in context.reader_feedback.confirmed_signals[:8]:
                signal_key = str(signal.signal_key or "").strip()
                if not signal_key:
                    continue
                add_evidence(
                    f"audience_signal:{signal_key}",
                    "audience_signal",
                    (
                        f"{signal.target_name or '整体'}:{signal.signal_type}:"
                        f"{signal.level}, hits={signal.hit_count}, severity={signal.max_severity}"
                    ),
                )

        return {
            "chapter": {
                "number": context.chapter_number,
                "title": context.chapter_plan_title,
                "one_line": context.chapter_plan_one_line,
                "goals": list(context.chapter_goals),
            },
            "overlay": {
                "reader_promise": (
                    context.reader_promise.model_dump(mode="json")
                    if context.reader_promise is not None
                    else {}
                ),
                "arc_payoff_map": (
                    context.arc_payoff_map.model_dump(mode="json")
                    if context.arc_payoff_map is not None
                    else {}
                ),
                "band_delight_schedule": (
                    context.band_delight_schedule.model_dump(mode="json")
                    if context.band_delight_schedule is not None
                    else {}
                ),
                "chapter_experience_plan": (
                    context.chapter_experience_plan.model_dump(mode="json")
                    if context.chapter_experience_plan is not None
                    else {}
                ),
            },
            "world": {
                "timeline": context.timeline.model_dump(mode="json") if context.timeline is not None else {},
                "world_pressure": (
                    context.world_pressure.model_dump(mode="json") if context.world_pressure is not None else {}
                ),
                "active_rules": [item.model_dump(mode="json") for item in context.active_rules[:5]],
                "world_context": context.world_context.model_dump(mode="json"),
            },
            "audience": {
                "reader_feedback": (
                    context.reader_feedback.model_dump(mode="json")
                    if context.reader_feedback is not None
                    else {}
                ),
                "confirmed_signals": (
                    [item.model_dump(mode="json") for item in context.reader_feedback.confirmed_signals]
                    if context.reader_feedback is not None
                    else []
                ),
                "audience_hints": (
                    context.audience_hints.model_dump(mode="json")
                    if context.audience_hints is not None
                    else {}
                ),
                "recent_review_notes": [item.model_dump(mode="json") for item in context.recent_review_notes[:5]],
            },
            "draft": {
                "title": writer_output.title,
                "summary": writer_output.end_of_chapter_summary,
                "body_head": _trim(writer_output.body[:500], 500),
                "body_tail": _trim(writer_output.body[-500:], 500),
                "scene_outputs": [item.model_dump(mode="json") for item in writer_output.scene_outputs[:4]],
                "new_events": [item.model_dump(mode="json") for item in writer_output.new_events[:5]],
                "thread_beats": [item.model_dump(mode="json") for item in writer_output.thread_beats[:4]],
                "state_changes": [item.model_dump(mode="json") for item in writer_output.state_changes[:5]],
                "time_advance": (
                    writer_output.time_advance.model_dump(mode="json")
                    if writer_output.time_advance is not None
                    else None
                ),
            },
            "evidence_index": evidence_index,
        }

    def _llm_review_messages(
        self,
        *,
        payload: dict[str, Any],
        evidence_ids: list[str],
        reviewer_skill_layers: list[object] | None = None,
    ) -> list[dict[str, str]]:
        base_messages = [
            {
                "role": "system",
                "content": (
                    "你是 Web-Novel Experience Reviewer。只输出 JSON。"
                    "你审查的是网文体验而不是文学腔：看爽点兑现、问题梯子、沉浸感、规则可读性、"
                    "拖感是否仍有推进。所有 warn/fail issue 必须引用给定 evidence_id。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请根据下面数据审查当前章节，只返回 JSON 对象。\n"
                    "顶层字段必须包含：verdict、planned_reward_tags、delivered_reward_tags、"
                    "experience_scores、issues、review_notes、repair_instruction、evidence_refs、review_summary。\n"
                    "experience_scores 固定包含：narrative_understanding、attentional_focus、"
                    "emotional_engagement、narrative_presence、payoff_delivery、stall_tolerance、hook_efficiency，"
                    "分值 0 到 1。\n"
                    "issues 每项必须包含：rule_name、severity、description、issue_type、target_scope、"
                    "evidence_refs、suggested_fix。warn/fail issue 的 evidence_refs 必须全部来自允许列表。\n"
                    "repair_instruction 可以为 null，否则必须包含：repair_scope(draft/chapter_plan/band_plan)、"
                    "failure_type、must_fix、must_preserve、design_patch、evidence_refs。\n"
                    "若章节拖但仍有问题梯子/微进展/关系变化/规则稳态，只能给 warn 或 pass，不能给 fail。\n"
                    "只有当 no-payoff stall 连续成立，或出现因果/stakes/规则可读性断裂且能指向证据时，才给 fail。\n"
                    f"允许引用的 evidence_id：{json.dumps(evidence_ids, ensure_ascii=False)}\n"
                    f"审查数据：{json.dumps(payload, ensure_ascii=False)}"
                ),
            },
        ]
        return inject_skill_layers(base_messages, reviewer_skill_layers or [])

    def _repair_llm_json(
        self,
        *,
        raw: str,
        error: str,
        evidence_ids: list[str],
    ) -> str | None:
        try:
            return call_chat_compat(
                self.llm_client,
                [
                    {
                        "role": "system",
                        "content": "你是 JSON repair 工具。只输出修复后的 JSON 对象，不要解释。",
                    },
                    {
                        "role": "user",
                        "content": (
                            "上一轮 WNER 输出无法解析或证据引用非法。请修复为合法 JSON，保留原本审查语义。\n"
                            "所有 warn/error issue 的 evidence_refs 必须非空，且每个 ref 必须来自允许列表。\n"
                            "如果无法确认证据，请删除该 issue 或降级为不带 issue 的 pass/warn 摘要。\n"
                            f"错误：{error}\n"
                            f"允许 evidence_id：{json.dumps(evidence_ids, ensure_ascii=False)}\n"
                            f"原始输出：{raw}"
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=3000,
                timeout_seconds=30,
                retry_on_timeout=False,
                task_family="reviewer",
                stage_key="chapter_review_json_repair",
                output_schema={"type": "object"},
            )
        except Exception:
            return None

    def _verdict_from_payload(
        self,
        *,
        payload: dict[str, Any],
        context: ReviewContextPack,
        writer_output: WriterOutput,
        fallback_on_invalid: bool,
        allowed_evidence_ids: set[str] | None = None,
    ) -> ReviewVerdict:
        heuristic = self._review_with_heuristics(context, writer_output)
        scores = payload.get("experience_scores") or {}
        planned_tags = [
            str(item) for item in (payload.get("planned_reward_tags") or heuristic.planned_reward_tags) if str(item).strip()
        ]
        delivered_tags = [
            str(item) for item in (payload.get("delivered_reward_tags") or heuristic.delivered_reward_tags) if str(item).strip()
        ]
        issues: list[ContinuityIssue] = []
        raw_issues = payload.get("issues") or []
        for item in raw_issues if isinstance(raw_issues, list) else []:
            if not isinstance(item, dict):
                continue
            evidence_refs = [str(ref) for ref in (item.get("evidence_refs") or []) if str(ref).strip()]
            severity = self._normalize_issue_severity(str(item.get("severity") or "warning"))
            invalid_refs = [
                ref for ref in evidence_refs if allowed_evidence_ids is not None and ref not in allowed_evidence_ids
            ]
            audience_only = bool(evidence_refs) and all(
                str(ref).startswith("audience_signal:") for ref in evidence_refs
            )
            if severity in {"warning", "error"} and (not evidence_refs or invalid_refs):
                if fallback_on_invalid:
                    return heuristic
                raise ValueError("LLM issue missing or invalid evidence refs")
            if severity == "error" and audience_only:
                if fallback_on_invalid:
                    return heuristic
                raise ValueError("LLM hard fail cannot be based only on audience signals")
            issues.append(
                ContinuityIssue(
                    rule_name=str(item.get("rule_name") or "wner_issue"),
                    severity=severity,
                    description=str(item.get("description") or "体验问题"),
                    reviewer="webnovel_experience",
                    issue_type=str(item.get("issue_type") or "experience"),
                    target_scope=str(item.get("target_scope") or "chapter"),
                    evidence_refs=evidence_refs,
                    suggested_fix=str(item.get("suggested_fix") or ""),
                )
            )
        verdict = str(payload.get("verdict") or heuristic.verdict)
        if verdict not in {"pass", "warn", "fail"}:
            if fallback_on_invalid:
                return heuristic
            raise ValueError("Invalid verdict")
        repair_instruction = None
        raw_repair = payload.get("repair_instruction")
        if isinstance(raw_repair, dict) and raw_repair:
            repair_instruction = RepairInstruction.model_validate(raw_repair)
            if repair_instruction.repair_scope == "arc":
                scope_reason = repair_instruction.scope_reason or "ordinary review cannot escalate above band repair"
                repair_instruction = repair_instruction.model_copy(
                    update={"repair_scope": "band", "scope_reason": scope_reason}
                )
        elif verdict == "fail":
            repair_instruction = heuristic.repair_instruction
        review_notes = [
            str(item) for item in (payload.get("review_notes") or heuristic.review_notes) if str(item).strip()
        ]
        evidence_refs = [str(item) for item in (payload.get("evidence_refs") or []) if str(item).strip()]
        if allowed_evidence_ids is not None:
            evidence_refs = [item for item in evidence_refs if item in allowed_evidence_ids]
        if not evidence_refs:
            evidence_refs = heuristic.evidence_refs
        verdict_model = ReviewVerdict(
            verdict=verdict,  # type: ignore[arg-type]
            issues=issues or heuristic.issues if verdict != "pass" else issues,
            recommended_action=(
                "rewrite" if verdict == "fail" else "pause_for_review" if verdict == "warn" else "continue"
            ),
            review_summary=str(payload.get("review_summary") or heuristic.review_summary),
            planned_reward_tags=planned_tags or heuristic.planned_reward_tags,
            delivered_reward_tags=delivered_tags or heuristic.delivered_reward_tags,
            experience_scores={
                "narrative_understanding": float(scores.get("narrative_understanding", heuristic.experience_scores.get("narrative_understanding", 0.5))),
                "attentional_focus": float(scores.get("attentional_focus", heuristic.experience_scores.get("attentional_focus", 0.5))),
                "emotional_engagement": float(scores.get("emotional_engagement", heuristic.experience_scores.get("emotional_engagement", 0.5))),
                "narrative_presence": float(scores.get("narrative_presence", heuristic.experience_scores.get("narrative_presence", 0.5))),
                "payoff_delivery": float(scores.get("payoff_delivery", heuristic.experience_scores.get("payoff_delivery", 0.5))),
                "stall_tolerance": float(scores.get("stall_tolerance", heuristic.experience_scores.get("stall_tolerance", 0.5))),
                "hook_efficiency": float(scores.get("hook_efficiency", heuristic.experience_scores.get("hook_efficiency", 0.5))),
            },
            review_notes=review_notes,
            lint_signals=list(context.lint_signals),
            evidence_refs=evidence_refs,
            confirmed_signal_refs=self._confirmed_signal_refs(context),
            reviewer_mode="llm",
            repair_instruction=repair_instruction,
        )
        return self._anchor_verdict_evidence(verdict_model, context, writer_output)

    @staticmethod
    def _normalize_issue_severity(severity: str) -> str:
        normalized = severity.strip().lower()
        if normalized in {"error", "fail", "failure", "fatal"}:
            return "error"
        if normalized in {"info", "notice"}:
            return "info"
        return "warning"

    def _anchor_verdict_evidence(
        self,
        verdict: ReviewVerdict,
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> ReviewVerdict:
        allowed_ids = [
            str(item.get("evidence_id") or "")
            for item in self._llm_payload(context, writer_output).get("evidence_index", [])
            if str(item.get("evidence_id") or "").strip()
        ]
        allowed_set = set(allowed_ids)
        if not allowed_set:
            return verdict

        anchored_issues: list[ContinuityIssue] = []
        for issue in verdict.issues:
            refs = [ref for ref in issue.evidence_refs if ref in allowed_set]
            if issue.severity in {"warning", "error"} and not refs:
                refs = self._fallback_issue_evidence_refs(issue, allowed_ids)
            anchored_issues.append(issue.model_copy(update={"evidence_refs": refs}))

        evidence_refs = [ref for ref in verdict.evidence_refs if ref in allowed_set]
        if not evidence_refs:
            evidence_refs = list(
                dict.fromkeys(ref for issue in anchored_issues for ref in issue.evidence_refs if ref in allowed_set)
            )[:12]
        if not evidence_refs:
            evidence_refs = allowed_ids[:1]

        repair_instruction = verdict.repair_instruction
        if repair_instruction is not None:
            repair_refs = [ref for ref in repair_instruction.evidence_refs if ref in allowed_set]
            if not repair_refs:
                repair_refs = evidence_refs[:12]
            repair_instruction = repair_instruction.model_copy(update={"evidence_refs": repair_refs})

        return verdict.model_copy(
            update={
                "issues": anchored_issues,
                "evidence_refs": evidence_refs[:12],
                "repair_instruction": repair_instruction,
            }
        )

    def _fallback_issue_evidence_refs(
        self,
        issue: ContinuityIssue,
        allowed_ids: list[str],
    ) -> list[str]:
        def first_matching(prefixes: tuple[str, ...]) -> list[str]:
            return [item for item in allowed_ids if item.startswith(prefixes)][:3]

        if issue.issue_type == "lint":
            refs = first_matching(("lint:",))
            if refs:
                return refs
        if issue.issue_type == "stall" or issue.target_scope == "band":
            refs = first_matching(("overlay:band_delight_schedule", "review_note:"))
            if refs:
                return refs
        if issue.issue_type == "payoff_miss":
            refs = first_matching(("scene:", "draft_event:", "thread:", "state:"))
            refs.extend(
                item
                for item in allowed_ids
                if item in {"overlay:chapter_experience_plan", "overlay:band_delight_schedule"}
            )
            if refs:
                return list(dict.fromkeys(refs))[:3]
        if issue.issue_type == "hook_failure":
            refs = first_matching(("draft:body_tail", "draft:summary", "scene:"))
            if refs:
                return refs
        if issue.issue_type == "immersion":
            refs = first_matching(("active_rule:", "rule_event:", "overlay:chapter_experience_plan", "overlay:arc_payoff_map"))
            if refs:
                return refs
        refs = first_matching(("draft:", "scene:", "overlay:chapter_experience_plan", "overlay:band_delight_schedule"))
        return refs or allowed_ids[:1]

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

    def _map_movement_issue(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> ContinuityIssue | None:
        if len(writer_output.scene_outputs) < 2:
            return None
        map_context = context.map_context or {}
        review_graph = map_context.get("review_graph") if isinstance(map_context.get("review_graph"), dict) else {}
        node_payloads = review_graph.get("map_nodes") if isinstance(review_graph.get("map_nodes"), list) else []
        edge_payloads = review_graph.get("map_edges") if isinstance(review_graph.get("map_edges"), list) else []
        if review_graph and review_graph.get("available") is False and (not node_payloads or not edge_payloads):
            return None
        if not node_payloads and not edge_payloads:
            node_payloads = map_context.get("map_nodes") if isinstance(map_context.get("map_nodes"), list) else []
            edge_payloads = map_context.get("map_edges") if isinstance(map_context.get("map_edges"), list) else []
        if not node_payloads or not edge_payloads:
            return None
        try:
            nodes = [MapNode.model_validate(payload) for payload in node_payloads if isinstance(payload, dict)]
            edges = [MapEdge.model_validate(payload) for payload in edge_payloads if isinstance(payload, dict)]
        except Exception:
            return None
        objective_graph_payload = (
            map_context.get("objective_review_graph")
            if isinstance(map_context.get("objective_review_graph"), dict)
            else {}
        )
        objective_nodes = nodes
        objective_edges = edges
        if objective_graph_payload:
            objective_node_payloads = (
                objective_graph_payload.get("map_nodes")
                if isinstance(objective_graph_payload.get("map_nodes"), list)
                else []
            )
            objective_edge_payloads = (
                objective_graph_payload.get("map_edges")
                if isinstance(objective_graph_payload.get("map_edges"), list)
                else []
            )
            try:
                parsed_objective_nodes = [
                    MapNode.model_validate(payload)
                    for payload in objective_node_payloads
                    if isinstance(payload, dict)
                ]
                parsed_objective_edges = [
                    MapEdge.model_validate(payload)
                    for payload in objective_edge_payloads
                    if isinstance(payload, dict)
                ]
            except Exception:
                parsed_objective_nodes = []
                parsed_objective_edges = []
            if parsed_objective_nodes and parsed_objective_edges:
                objective_nodes = parsed_objective_nodes
                objective_edges = parsed_objective_edges
        node_by_id = {node.id: node for node in nodes}
        node_id_by_name = {node.name: node.id for node in nodes if node.name}
        graph = MapGraph(nodes=nodes, edges=edges)
        objective_graph = MapGraph(nodes=objective_nodes, edges=objective_edges)
        cognition_by_observer = self._observer_cognition_views(map_context)
        movement_policy = self._movement_policy(map_context)

        ordered_scenes = sorted(writer_output.scene_outputs, key=lambda scene: scene.scene_no)
        path_refs: list[str] = []
        total_travel_time = 0.0
        used_observer_known_path = False
        for previous, current in zip(ordered_scenes, ordered_scenes[1:]):
            previous_id = self._resolve_scene_location_id(previous.scene_location_id, node_by_id, node_id_by_name)
            current_id = self._resolve_scene_location_id(current.scene_location_id, node_by_id, node_id_by_name)
            if not previous_id or not current_id or previous_id == current_id:
                continue
            observer = self._scene_observer(previous, current, map_context)
            objective_result = objective_graph.shortest_path(previous_id, current_id, metric="travel_time")
            known_result = None
            cognition = cognition_by_observer.get(observer) if observer is not None else None
            if cognition is not None:
                known_graph = MapGraph(
                    nodes=objective_nodes,
                    edges=objective_edges,
                    cognition_by_observer={observer: cognition},
                )
                known_result = known_graph.shortest_path(
                    previous_id,
                    current_id,
                    metric="travel_time",
                    observer=observer,
                )
                used_observer_known_path = True
            result = known_result or graph.shortest_path(previous_id, current_id, metric="travel_time")
            if not objective_result.reachable:
                blocked_result = objective_graph.shortest_path(
                    previous_id,
                    current_id,
                    metric="travel_time",
                    allow_blocked=True,
                )
                if blocked_result.reachable:
                    return self._map_path_issue(
                        rule_name="map_blocked_route_unusable",
                        description="相邻场景依赖的地图路线存在，但当前处于 blocked/sealed/destroyed 状态。",
                        previous_scene_no=previous.scene_no,
                        current_scene_no=current.scene_no,
                        previous_id=previous_id,
                        current_id=current_id,
                        evidence_refs=[
                            f"scene:{previous.scene_no}->{current.scene_no}",
                            f"from={previous_id}",
                            f"to={current_id}",
                            f"blocked_path={','.join(blocked_result.path_edge_ids)}",
                        ],
                        suggested_fix="补出解封/绕行过程，或调整为当前可通行路线。",
                    )
                return self._map_path_issue(
                    rule_name="map_path_unreachable",
                    description="相邻场景发生地点切换，但 objective 地图图中没有可达路线。",
                    previous_scene_no=previous.scene_no,
                    current_scene_no=current.scene_no,
                    previous_id=previous_id,
                    current_id=current_id,
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"from={previous_id}",
                        f"to={current_id}",
                        f"blocked_reason={objective_result.blocked_reason}",
                    ],
                    suggested_fix="调整场景地点、补充合理赶路过程，或在地图中添加 objective 可达路线。",
                )
            if known_result is not None and not known_result.reachable:
                hidden_unseen = self._path_uses_hidden_edge(objective_result.path_edge_ids, objective_graph, cognition)
                return self._map_path_issue(
                    rule_name="map_hidden_route_unseen" if hidden_unseen else "map_known_path_unreachable",
                    description=(
                        "objective 路线可达，但当前观察者尚不知道关键隐藏路线。"
                        if hidden_unseen
                        else "objective 路线可达，但当前观察者认知图中没有可达路线。"
                    ),
                    previous_scene_no=previous.scene_no,
                    current_scene_no=current.scene_no,
                    previous_id=previous_id,
                    current_id=current_id,
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"observer={observer[0]}:{observer[1]}",
                        f"objective_path={','.join(objective_result.path_edge_ids)}",
                        f"known_blocked_reason={known_result.blocked_reason}",
                    ],
                    suggested_fix="让角色先发现/确认路线，改走已知路线，或补出误入隐藏路线的叙事因果。",
                )
            if known_result is not None and self._path_uses_false_edge(known_result.path_edge_ids, cognition):
                return self._map_path_issue(
                    rule_name="map_false_route_used",
                    description="章节移动采用了观察者认知中的 false route，缺少被骗或修正的叙事处理。",
                    previous_scene_no=previous.scene_no,
                    current_scene_no=current.scene_no,
                    previous_id=previous_id,
                    current_id=current_id,
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"observer={observer[0]}:{observer[1]}",
                        f"known_path={','.join(known_result.path_edge_ids)}",
                    ],
                    suggested_fix="改为真实可达路线，或把 false route 写成误导并付出绕行/失败代价。",
                )
            if not result.reachable:
                return ContinuityIssue(
                    rule_name="map_path_unreachable",
                    severity="error",
                    description="相邻场景发生地点切换，但地图图中没有可达路线。",
                    reviewer="webnovel_experience",
                    issue_type="continuity",
                    target_scope="scene",
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"from={previous_id}",
                        f"to={current_id}",
                        f"blocked_reason={result.blocked_reason}",
                    ],
                    suggested_fix="调整场景地点、补充合理赶路过程，或在地图中添加可达路线。",
                )
            unmet_access_rule = self._path_unmet_access_rule(
                result.path_edge_ids,
                objective_graph,
                movement_policy,
            )
            if unmet_access_rule is not None:
                edge_id, access_rule_id = unmet_access_rule
                return self._map_path_issue(
                    rule_name="map_access_rule_unmet",
                    description="相邻场景使用了当前 movement policy 未授权的地图通行规则。",
                    previous_scene_no=previous.scene_no,
                    current_scene_no=current.scene_no,
                    previous_id=previous_id,
                    current_id=current_id,
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"from={previous_id}",
                        f"to={current_id}",
                        f"edge_id={edge_id}",
                        f"access_rule_id={access_rule_id}",
                        f"path={','.join(result.path_edge_ids)}",
                    ],
                    suggested_fix="补出通行凭证/权限获取，改走已授权路线，或更新 reviewer-only movement policy。",
                )
            effective_travel_time = self._effective_path_travel_time(result, objective_graph, movement_policy)
            total_travel_time += effective_travel_time
            path_refs.append(
                f"scene:{previous.scene_no}->{current.scene_no}:travel_time={round(effective_travel_time, 3)}:raw_travel_time={result.total_travel_time}:path={','.join(result.path_edge_ids)}"
            )

        if total_travel_time <= 0:
            return None
        budget = self._chapter_travel_time_budget(context, writer_output)
        if budget is None or total_travel_time <= budget:
            return None
        return ContinuityIssue(
            rule_name=(
                "map_known_travel_time_exceeds_chapter_time"
                if used_observer_known_path
                else "map_travel_time_exceeds_chapter_time"
            ),
            severity="error",
            description=(
                "角色按 observer-known 路径移动所需赶路时间超过本章时间推进。"
                if used_observer_known_path
                else "角色场景移动所需地图赶路时间超过本章时间推进。"
            ),
            reviewer="webnovel_experience",
            issue_type="continuity",
            target_scope="scene",
            evidence_refs=[
                f"required_travel_time={round(total_travel_time, 3)}",
                f"available_time={round(budget, 3)}",
                *path_refs[:4],
            ],
            suggested_fix="延长章节时间推进、改为更近地点、使用已知快速路线，或补出合理中转。",
        )

    @staticmethod
    def _observer_cognition_views(map_context: dict) -> dict[tuple[str, str], CognitionView]:
        payload = map_context.get("observer_cognition")
        if not isinstance(payload, dict):
            return {}
        views: dict[tuple[str, str], CognitionView] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            try:
                overlay = CognitionOverlay.model_validate(value)
            except Exception:
                continue
            observer_key = (str(overlay.observer_type), overlay.observer_id)
            views[observer_key] = CognitionView(overlay)
        return views

    @staticmethod
    def _scene_observer(previous_scene, current_scene, map_context: dict) -> tuple[str, str] | None:
        active_locations = [
            item
            for item in map_context.get("active_locations", [])
            if isinstance(item, dict)
        ]
        if not active_locations:
            return None
        involved = [
            str(item).strip()
            for item in [
                *list(getattr(previous_scene, "involved_entities", []) or []),
                *list(getattr(current_scene, "involved_entities", []) or []),
            ]
            if str(item).strip()
        ]
        for entity_ref in involved:
            for active in active_locations:
                entity_id = str(active.get("entity_id", "") or "").strip()
                entity_name = str(active.get("entity_name", "") or "").strip()
                if entity_ref and (entity_ref == entity_id or entity_ref == entity_name):
                    return ("character", entity_id or entity_ref)
        return None

    @staticmethod
    def _path_uses_hidden_edge(
        edge_ids: list[str],
        graph: MapGraph,
        cognition: CognitionView | None,
    ) -> bool:
        hidden_refs = cognition.hidden_refs if cognition is not None else set()
        for edge_id in edge_ids:
            base_edge_id = edge_id.removesuffix("__reverse")
            edge = graph.edges_by_id.get(edge_id) or graph.edges_by_id.get(base_edge_id)
            if edge is None:
                continue
            edge_ref = f"map_edge:{base_edge_id}"
            if edge_ref in hidden_refs:
                return True
            if edge.status == "hidden" or edge.edge_type == "hidden_route" or not edge.discovered_by_default:
                return True
        return False

    @staticmethod
    def _path_uses_false_edge(edge_ids: list[str], cognition: CognitionView | None) -> bool:
        if cognition is None:
            return False
        false_ids: set[str] = set()
        for key, value in cognition.false_edges.items():
            false_ids.add(str(key))
            false_ids.add(str(key).removeprefix("map_edge:"))
            if isinstance(value, MapEdge):
                false_ids.add(value.id)
        return any(edge_id in false_ids or edge_id.removesuffix("__reverse") in false_ids for edge_id in edge_ids)

    @staticmethod
    def _movement_policy(map_context: dict) -> dict[str, Any]:
        policy = map_context.get("movement_policy")
        return dict(policy) if isinstance(policy, dict) else {}

    @staticmethod
    def _path_unmet_access_rule(
        edge_ids: list[str],
        graph: MapGraph,
        movement_policy: dict[str, Any],
    ) -> tuple[str, str] | None:
        if "allowed_access_rule_ids" not in movement_policy:
            return None
        raw_allowed = movement_policy.get("allowed_access_rule_ids")
        allowed = {
            str(item).strip()
            for item in (raw_allowed if isinstance(raw_allowed, list) else [])
            if str(item).strip()
        }
        for edge_id in edge_ids:
            edge = _edge_from_path_id(graph, edge_id)
            if edge is None:
                continue
            access_rule_id = str(edge.access_rule_id or "").strip()
            if access_rule_id and access_rule_id not in allowed:
                return edge.id, access_rule_id
        return None

    @staticmethod
    def _effective_path_travel_time(
        result,
        graph: MapGraph,
        movement_policy: dict[str, Any],
    ) -> float:
        edge_type_multipliers = movement_policy.get("travel_time_multiplier_by_edge_type")
        multipliers = edge_type_multipliers if isinstance(edge_type_multipliers, dict) else {}
        total = 0.0
        used_edges = False
        for edge_id in result.path_edge_ids:
            edge = _edge_from_path_id(graph, edge_id)
            if edge is None:
                continue
            used_edges = True
            multiplier = _float_policy_value(multipliers.get(str(edge.edge_type)), default=1.0)
            total += float(edge.travel_time or 0.0) * multiplier
        if not used_edges:
            total = float(result.total_travel_time or 0.0)
        team_speed_multiplier = _float_policy_value(movement_policy.get("team_speed_multiplier"), default=1.0)
        if team_speed_multiplier <= 0:
            team_speed_multiplier = 1.0
        return total / team_speed_multiplier

    @staticmethod
    def _map_path_issue(
        *,
        rule_name: str,
        description: str,
        previous_scene_no: int,
        current_scene_no: int,
        previous_id: str,
        current_id: str,
        evidence_refs: list[str],
        suggested_fix: str,
    ) -> ContinuityIssue:
        return ContinuityIssue(
            rule_name=rule_name,
            severity="error",
            description=description,
            reviewer="webnovel_experience",
            issue_type="continuity",
            target_scope="scene",
            evidence_refs=evidence_refs
            or [
                f"scene:{previous_scene_no}->{current_scene_no}",
                f"from={previous_id}",
                f"to={current_id}",
            ],
            suggested_fix=suggested_fix,
        )

    @staticmethod
    def _resolve_scene_location_id(
        raw_location: str,
        node_by_id: dict[str, MapNode],
        node_id_by_name: dict[str, str],
    ) -> str:
        text = str(raw_location or "").strip()
        if not text:
            return ""
        if text in node_by_id:
            return text
        return node_id_by_name.get(text, "")

    @staticmethod
    def _chapter_travel_time_budget(
        context: ReviewContextPack,
        writer_output: WriterOutput,
    ) -> float | None:
        map_context = context.map_context or {}
        explicit_budget = map_context.get("chapter_travel_time_budget")
        if explicit_budget is not None:
            try:
                return max(0.0, float(explicit_budget))
            except (TypeError, ValueError):
                pass
        if writer_output.time_advance is None:
            return None
        return _duration_to_travel_time_budget(writer_output.time_advance.duration_description)

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
            llm_decision = self._choose_repair_escalation_with_llm(
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

    def _choose_repair_escalation_with_llm(
        self,
        *,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        review: ReviewVerdict,
        repair_attempts: list[dict[str, object]],
        base_instruction: RepairInstruction,
    ) -> RepairInstruction | None:
        payload = self._repair_escalation_payload(
            context=context,
            writer_output=writer_output,
            review=review,
            repair_attempts=repair_attempts,
        )
        try:
            raw = self.llm_client.chat(
                self._llm_escalation_messages(payload=payload),
                temperature=0.0,
                max_tokens=1600,
                timeout_seconds=30,
                retry_on_timeout=True,
            )
            parsed = parse_llm_json(raw, error_prefix="WNESC")
        except (LLMJSONParseError, Exception):
            return None

        scope = str(parsed.get("repair_scope") or "").strip().lower()
        if scope not in {"band", "arc"}:
            return None
        scope_reason = str(parsed.get("scope_reason") or "").strip()
        design_patch = parsed.get("design_patch") if isinstance(parsed.get("design_patch"), dict) else {}
        merged_design_patch = dict(base_instruction.design_patch)
        merged_design_patch.update(design_patch)
        evidence_refs = [
            str(item)
            for item in (parsed.get("evidence_refs") or [])
            if str(item).strip()
        ]
        if not evidence_refs:
            evidence_refs = list(base_instruction.evidence_refs or review.evidence_refs)
        return base_instruction.model_copy(
            update={
                "repair_scope": scope,
                "scope_reason": scope_reason or "LLM reviewer selected third repair scope.",
                "design_patch": merged_design_patch,
                "evidence_refs": evidence_refs[:12],
            }
        )

    def _repair_escalation_payload(
        self,
        *,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        review: ReviewVerdict,
        repair_attempts: list[dict[str, object]],
    ) -> dict[str, Any]:
        return {
            "project_title": context.project_title,
            "chapter_number": context.chapter_number,
            "chapter_plan_title": context.chapter_plan_title,
            "chapter_plan_one_line": context.chapter_plan_one_line,
            "chapter_goals": list(context.chapter_goals[:3]),
            "review_summary": review.review_summary,
            "issues": [
                {
                    "rule_name": issue.rule_name,
                    "severity": issue.severity,
                    "description": issue.description,
                    "issue_type": issue.issue_type,
                    "target_scope": issue.target_scope,
                    "evidence_refs": list(issue.evidence_refs[:4]),
                }
                for issue in review.issues
            ],
            "repair_attempts": [
                {
                    "attempt_no": int(item.get("attempt_no", 0) or 0),
                    "repair_scope": str(item.get("repair_scope", "") or ""),
                    "result_verdict": str(item.get("result_verdict", "") or ""),
                }
                for item in repair_attempts
            ],
            "band_delight_schedule": (
                context.band_delight_schedule.model_dump(mode="json")
                if context.band_delight_schedule is not None
                else None
            ),
            "arc_payoff_map": (
                context.arc_payoff_map.model_dump(mode="json")
                if context.arc_payoff_map is not None
                else None
            ),
            "next_band_summary": (
                context.next_band_summary.model_dump(mode="json")
                if context.next_band_summary is not None
                else None
            ),
            "writer_output": {
                "title": writer_output.title,
                "end_of_chapter_summary": writer_output.end_of_chapter_summary,
                "scene_outputs": [item.model_dump(mode="json") for item in writer_output.scene_outputs[:3]],
                "new_events": [item.model_dump(mode="json") for item in writer_output.new_events[:4]],
                "thread_beats": [item.model_dump(mode="json") for item in writer_output.thread_beats[:4]],
                "state_changes": [item.model_dump(mode="json") for item in writer_output.state_changes[:4]],
            },
        }

    def _llm_escalation_messages(
        self,
        *,
        payload: dict[str, Any],
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "你是第三次 repair 的升级决策器。只输出 JSON。"
                    "你只能在 band 和 arc 两个 scope 中二选一。"
                    "arc 只表示需要改 arc 指导下的当前 band 计划，再重写当前章；"
                    "不要把普通节奏、hook、scene 执行问题升级成 arc。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请判断第三次 repair 应继续停留在 band，还是升级到 arc。"
                    "只返回 JSON 对象，字段必须包含：repair_scope、scope_reason、design_patch、evidence_refs。\n"
                    "只有当当前失败说明本 band 的任务分工已经受 arc 级 payoff / long-range structure 误导时，才允许 repair_scope=arc。\n"
                    "如果只是正文执行、章内节奏、局部承诺兑现、当前 band 排布问题，必须返回 band。\n"
                    f"决策数据：{json.dumps(payload, ensure_ascii=False)}"
                ),
            },
        ]

    @staticmethod
    def _infer_tags_from_text(text: str) -> list[str]:
        normalized = str(text or "")
        matched: list[str] = []
        for tag, keywords in _TAG_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                matched.append(tag)
        return matched
