from __future__ import annotations

import json
from typing import Any

from forwin.llm.compat import call_chat_compat
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure
from forwin.protocol.context import ReviewContextPack
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.skills import inject_skill_layers
from forwin.utils import LLMJSONParseError, parse_llm_json


def _trim(text: str, limit: int = 64) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)] + "…"


class LLMWebNovelReviewer:
    name = "llm_webnovel"

    def __init__(
        self,
        *,
        llm_client=None,
        heuristic_reviewer=None,
        enabled: bool = True,
    ) -> None:
        self.llm_client = llm_client
        self.heuristic_reviewer = heuristic_reviewer
        self.enabled = enabled
        self._last_attempts: list[dict[str, object]] = []

    def review(
        self,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        *,
        reviewer_skill_layers: list[object] | None = None,
    ) -> ReviewVerdict | None:
        if not self.enabled or self.llm_client is None:
            return None
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
            self._last_attempts = self._drain_llm_attempts()
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
                attempts = self._drain_llm_attempts()
                self._last_attempts = attempts
                return verdict.model_copy(
                    update={
                        "prompt_trace": self._build_prompt_trace(
                            context=context,
                            writer_output=writer_output,
                            messages=messages,
                            attempts=attempts,
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
                    self._last_attempts = self._drain_llm_attempts()
                    return None
                repaired = self._repair_llm_json(
                    raw=raw,
                    error=str(exc),
                    evidence_ids=evidence_ids,
                )
                if repaired is None:
                    self._last_attempts = self._drain_llm_attempts()
                    return None
                raw = repaired
            except Exception:
                self._last_attempts = self._drain_llm_attempts()
                return None
        self._last_attempts = self._drain_llm_attempts()
        return None

    def fallback_trace(
        self,
        *,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        heuristic: ReviewVerdict,
    ) -> dict[str, object] | None:
        if not self._last_attempts:
            return None
        return self._build_prompt_trace(
            context=context,
            writer_output=writer_output,
            messages=[],
            attempts=list(self._last_attempts),
            output_summary={
                "status": "failed",
                "fallback": "heuristic",
                "verdict": heuristic.verdict,
                "issue_count": len(heuristic.issues),
            },
        )

    def anchor_verdict_evidence(
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

    def choose_repair_escalation(
        self,
        *,
        context: ReviewContextPack,
        writer_output: WriterOutput,
        review: ReviewVerdict,
        repair_attempts: list[dict[str, object]],
        base_instruction: RepairInstruction,
    ) -> RepairInstruction | None:
        if not self.enabled or self.llm_client is None:
            return None
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

    def _heuristic_verdict(self, context: ReviewContextPack, writer_output: WriterOutput) -> ReviewVerdict:
        heuristic = getattr(self.heuristic_reviewer, "_review_with_heuristics", None)
        if callable(heuristic):
            return heuristic(context, writer_output)
        return ReviewVerdict(verdict="pass", issues=[])

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
        for item in context.active_personality_contexts[:8]:
            character_id = str(item.get("character_id") or item.get("character_name") or "").strip()
            if character_id:
                add_evidence(f"personality:{character_id}", "personality", json.dumps(item, ensure_ascii=False))

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
        deterministic_quality_report = (
            context.deterministic_quality_report
            if isinstance(getattr(context, "deterministic_quality_report", None), dict)
            else {}
        )
        for signal in (
            list(deterministic_quality_report.get("blocking_signals", []) or [])
            + list(deterministic_quality_report.get("warning_signals", []) or [])
        )[:12]:
            if not isinstance(signal, dict):
                continue
            signal_id = str(signal.get("signal_id") or "").strip()
            if not signal_id:
                continue
            add_evidence(
                f"canon_quality:{signal_id}",
                "canon_quality_signal",
                str(signal.get("description") or signal.get("signal_type") or ""),
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
            "personality": list(context.active_personality_contexts[:8]),
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
            "deterministic_quality_report": deterministic_quality_report,
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
                    "同时检查人物是否符合 active_personality_context，但人格 skill 不能覆盖 canon。"
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
                    "人物一致性检查：决策是否符合 dominant skill；对白是否符合 expression/social mask；"
                    "压力反应是否符合 stress mode；是否把倾向误写成绝对规则；是否由 skill 发明新 canon。\n"
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
        heuristic = self._heuristic_verdict(context, writer_output)
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
        return self.anchor_verdict_evidence(verdict_model, context, writer_output)

    @staticmethod
    def _normalize_issue_severity(severity: str) -> str:
        normalized = severity.strip().lower()
        if normalized in {"error", "fail", "failure", "fatal"}:
            return "error"
        if normalized in {"info", "notice"}:
            return "info"
        return "warning"

    @staticmethod
    def _confirmed_signal_refs(context: ReviewContextPack) -> list[str]:
        feedback = context.reader_feedback
        if feedback is None:
            return []
        refs: list[str] = []
        for signal in feedback.confirmed_signals:
            key = str(signal.signal_key or "").strip()
            if key:
                refs.append(f"audience_signal:{key}")
        return refs

    @staticmethod
    def _fallback_issue_evidence_refs(
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

    @staticmethod
    def _repair_escalation_payload(
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

    @staticmethod
    def _llm_escalation_messages(
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
