from __future__ import annotations

from collections import Counter
import json
import re
from typing import Any

from forwin.protocol.context import ChapterContextPack, LintSignal, ReviewContextPack
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.skills import inject_skill_layers
from forwin.utils import LLMJSONParseError, parse_llm_json
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
            return ReviewVerdict(verdict="pass", issues=[])

        if self.llm_enabled and self.llm_client is not None:
            llm_verdict = self._review_with_llm(
                review_context,
                writer_output,
                reviewer_skill_layers=reviewer_skill_layers,
            )
            if llm_verdict is not None:
                return llm_verdict
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
        try:
            raw = self.llm_client.chat(
                self._llm_review_messages(
                    payload=payload,
                    evidence_ids=evidence_ids,
                    reviewer_skill_layers=reviewer_skill_layers,
                ),
                temperature=0.1,
                max_tokens=3000,
                timeout_seconds=45,
                retry_on_timeout=True,
            )
        except Exception:
            return None

        allowed_evidence_ids = set(evidence_ids)
        for repair_attempt in range(3):
            try:
                parsed = parse_llm_json(raw, error_prefix="WNER")
                return self._verdict_from_payload(
                    payload=parsed,
                    context=context,
                    writer_output=writer_output,
                    fallback_on_invalid=False,
                    allowed_evidence_ids=allowed_evidence_ids,
                )
            except (LLMJSONParseError, ValueError) as exc:
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
                    "repair_instruction 可以为 null，否则必须包含：repair_scope(scene/band/arc)、"
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
            return self.llm_client.chat(
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
        scope_rank = {"scene": 1, "band": 2, "arc": 3}
        scope = max(
            (issue.target_scope for issue in issues if issue.severity == "error"),
            key=lambda item: scope_rank.get(item, 1),
            default="scene",
        )
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
            design_patch=design_patch,
            evidence_refs=evidence_refs[:12],
        )

    @staticmethod
    def _infer_tags_from_text(text: str) -> list[str]:
        normalized = str(text or "")
        matched: list[str] = []
        for tag, keywords in _TAG_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                matched.append(tag)
        return matched
