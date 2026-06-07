from __future__ import annotations

import json

from forwin.protocol.review import RepairInstruction, RepairVerification, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.utils import parse_llm_json
from forwin.llm.compat import call_chat_compat

_MAX_REVIEW_ISSUES_FOR_LLM = 12
_MAX_TEXT_LIST_ITEMS_FOR_LLM = 8
_MAX_LLM_USER_CONTENT_CHARS = 24_000


def _meaningful_preserve_constraint(value: object) -> str:
    normalized = str(value or "").strip()
    if len(normalized) < 2:
        return ""
    return normalized


def _clip_text(value: object, limit: int, *, keep_excerpt: bool = True) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if not keep_excerpt:
        return f"<omitted {len(text)} chars>"
    return f"{text[:limit]}... <truncated {len(text) - limit} chars>"


def _compact_text_list(
    values: list[object] | tuple[object, ...] | None,
    *,
    max_items: int = _MAX_TEXT_LIST_ITEMS_FOR_LLM,
    limit: int = 240,
    keep_excerpt: bool = True,
) -> list[str]:
    items = [
        _clip_text(value, limit, keep_excerpt=keep_excerpt)
        for value in list(values or [])[:max_items]
        if str(value or "").strip()
    ]
    total = len(values or [])
    if total > max_items:
        items.append(f"<omitted {total - max_items} more items>")
    return items


def _compact_patch(value: dict[str, object], *, max_items: int = 10) -> dict[str, object]:
    compact: dict[str, object] = {}
    for index, (key, item) in enumerate((value or {}).items()):
        if index >= max_items:
            compact["<omitted_keys>"] = len(value) - max_items
            break
        if isinstance(item, dict):
            compact[str(key)] = _compact_patch(item, max_items=4)
        elif isinstance(item, list):
            compact[str(key)] = _compact_text_list(
                item,
                max_items=4,
                limit=180,
                keep_excerpt=False,
            )
        else:
            compact[str(key)] = _clip_text(item, 220, keep_excerpt=False)
    return compact


class RepairVerifier:
    def __init__(
        self,
        *,
        llm_client=None,
        llm_enabled: bool = False,
    ) -> None:
        self.llm_client = llm_client
        self.llm_enabled = bool(llm_enabled and llm_client is not None)

    def verify(
        self,
        *,
        original_output: WriterOutput,
        repaired_output: WriterOutput,
        before_review: ReviewVerdict,
        after_review: ReviewVerdict,
        repair_instruction: RepairInstruction,
    ) -> RepairVerification:
        rule_result = self._rule_verify(
            original_output=original_output,
            repaired_output=repaired_output,
            before_review=before_review,
            after_review=after_review,
            repair_instruction=repair_instruction,
        )
        if not self.llm_enabled:
            return rule_result
        llm_result = self._llm_verify(
            original_output=original_output,
            repaired_output=repaired_output,
            before_review=before_review,
            after_review=after_review,
            repair_instruction=repair_instruction,
        )
        if llm_result is None:
            return rule_result.model_copy(update={"verifier_mode": "rule_fallback"})
        if (
            rule_result.fixed_all_must_fix
            and rule_result.preserved_all_must_preserve
            and (
                not llm_result.fixed_all_must_fix
                or not llm_result.preserved_all_must_preserve
            )
        ):
            return rule_result.model_copy(
                update={
                    "new_risks": list(
                        dict.fromkeys([*rule_result.new_risks, *llm_result.new_risks])
                    ),
                    "verifier_mode": "rule_preferred_llm_disagreed",
                }
            )
        return RepairVerification(
            fixed_all_must_fix=(
                rule_result.fixed_all_must_fix and llm_result.fixed_all_must_fix
            ),
            preserved_all_must_preserve=(
                rule_result.preserved_all_must_preserve
                and llm_result.preserved_all_must_preserve
            ),
            unfixed=list(dict.fromkeys([*rule_result.unfixed, *llm_result.unfixed])),
            broken_preserve_constraints=list(
                dict.fromkeys(
                    [
                        *rule_result.broken_preserve_constraints,
                        *llm_result.broken_preserve_constraints,
                    ]
                )
            ),
            new_risks=list(dict.fromkeys([*rule_result.new_risks, *llm_result.new_risks])),
            verifier_mode="rule+llm",
        )

    @staticmethod
    def _issue_signature(issue) -> tuple[str, str, str, str]:
        entity_names = tuple(
            sorted(
                str(name or "").strip()
                for name in (getattr(issue, "entity_names", None) or [])
                if str(name or "").strip()
            )
        )
        identity = "\x1f".join(entity_names)
        if not identity:
            identity = str(getattr(issue, "description", "") or "").strip()
        return (
            str(getattr(issue, "rule_name", "") or "").strip(),
            str(getattr(issue, "issue_type", "") or "").strip(),
            str(getattr(issue, "target_scope", "") or "").strip(),
            identity,
        )

    @staticmethod
    def _issue_class_signature(issue) -> tuple[str, str, str]:
        return (
            str(getattr(issue, "rule_name", "") or "").strip(),
            str(getattr(issue, "issue_type", "") or "").strip(),
            str(getattr(issue, "target_scope", "") or "").strip(),
        )

    @staticmethod
    def _class_should_remain_same_until_fixed(issue) -> bool:
        rule_name = str(getattr(issue, "rule_name", "") or "").strip()
        issue_type = str(getattr(issue, "issue_type", "") or "").strip()
        persistent = {
            "countdown_non_monotonic",
            "countdown_stale_retrospective_reference",
            "final_countdown_unresolved",
        }
        return rule_name in persistent or issue_type in persistent

    def _rule_verify(
        self,
        *,
        original_output: WriterOutput,
        repaired_output: WriterOutput,
        before_review: ReviewVerdict,
        after_review: ReviewVerdict,
        repair_instruction: RepairInstruction,
    ) -> RepairVerification:
        before_errors = {
            self._issue_signature(issue): issue
            for issue in before_review.issues
            if str(issue.severity or "") == "error"
        }
        after_errors = {
            self._issue_signature(issue): issue
            for issue in after_review.issues
            if str(issue.severity or "") == "error"
        }
        after_errors_by_class: dict[tuple[str, str, str], list[tuple[tuple[str, str, str, str], object]]] = {}
        for key, issue in after_errors.items():
            after_errors_by_class.setdefault(self._issue_class_signature(issue), []).append((key, issue))
        persistent_unfixed_after_keys: set[tuple[str, str, str, str]] = set()
        unfixed = [
            str(before_errors[key].description or before_errors[key].rule_name or "").strip()
            for key in before_errors
            if key in after_errors
        ]
        for key, before_issue in before_errors.items():
            if key in after_errors or not self._class_should_remain_same_until_fixed(before_issue):
                continue
            for after_key, after_issue in after_errors_by_class.get(self._issue_class_signature(before_issue), []):
                persistent_unfixed_after_keys.add(after_key)
                unfixed.append(
                    str(getattr(after_issue, "description", "") or getattr(after_issue, "rule_name", "") or "").strip()
                )
                break
        new_risks = [
            str(after_errors[key].description or after_errors[key].rule_name or "").strip()
            for key in after_errors
            if key not in before_errors and key not in persistent_unfixed_after_keys
        ]
        broken_preserve_constraints: list[str] = []
        original_text = "\n".join(
            [
                str(original_output.title or ""),
                str(original_output.body or ""),
                str(original_output.end_of_chapter_summary or ""),
            ]
        )
        repaired_text = "\n".join(
            [
                str(repaired_output.title or ""),
                str(repaired_output.body or ""),
                str(repaired_output.end_of_chapter_summary or ""),
            ]
        )
        for item in repair_instruction.must_preserve:
            normalized = _meaningful_preserve_constraint(item)
            if normalized and normalized in original_text and normalized not in repaired_text:
                broken_preserve_constraints.append(f"修复后丢失保留内容：{normalized}")
        return RepairVerification(
            fixed_all_must_fix=not unfixed,
            preserved_all_must_preserve=not broken_preserve_constraints,
            unfixed=list(dict.fromkeys(unfixed)),
            broken_preserve_constraints=broken_preserve_constraints,
            new_risks=list(dict.fromkeys(new_risks)),
            verifier_mode="rule_only",
        )

    def _llm_verify(
        self,
        *,
        original_output: WriterOutput,
        repaired_output: WriterOutput,
        before_review: ReviewVerdict,
        after_review: ReviewVerdict,
        repair_instruction: RepairInstruction,
    ) -> RepairVerification | None:
        try:
            user_content = self._llm_prompt_payload(
                original_output=original_output,
                repaired_output=repaired_output,
                before_review=before_review,
                after_review=after_review,
                repair_instruction=repair_instruction,
            )
            raw = call_chat_compat(
                self.llm_client,
                [
                    {
                        "role": "system",
                        "content": (
                            "你是 repair verification 工具。只输出 JSON。"
                            "判断 must_fix 是否真正修复，must_preserve 是否被破坏。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": user_content,
                    },
                ],
                temperature=0.0,
                max_tokens=1600,
                timeout_seconds=30,
                retry_on_timeout=False,
                task_family="repair",
                stage_key="repair_verification",
                output_schema={"type": "object"},
            )
            payload = parse_llm_json(raw, error_prefix="Repair verification")
            verified = RepairVerification.model_validate(payload)
            return verified.model_copy(update={"verifier_mode": "llm_only"})
        except Exception:
            return None

    @classmethod
    def _llm_prompt_payload(
        cls,
        *,
        original_output: WriterOutput,
        repaired_output: WriterOutput,
        before_review: ReviewVerdict,
        after_review: ReviewVerdict,
        repair_instruction: RepairInstruction,
    ) -> str:
        payload = {
            "return_schema": {
                "fixed_all_must_fix": "bool",
                "preserved_all_must_preserve": "bool",
                "unfixed": "list[str]",
                "broken_preserve_constraints": "list[str]",
                "new_risks": "list[str]",
            },
            "repair_instruction": cls._compact_repair_instruction(repair_instruction),
            "before_review": cls._compact_review(before_review),
            "after_review": cls._compact_review(after_review),
            "original_draft": cls._compact_writer_output(original_output),
            "repaired_draft": cls._compact_writer_output(repaired_output),
        }
        content = (
            "只返回 JSON 对象。根据以下摘要判断 must_fix 是否已修复，"
            "must_preserve 是否被破坏；不要要求额外人工信息。\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )
        if len(content) <= _MAX_LLM_USER_CONTENT_CHARS:
            return content
        return (
            content[:_MAX_LLM_USER_CONTENT_CHARS]
            + f"... <repair verification payload truncated from {len(content)} chars>"
        )

    @staticmethod
    def _compact_repair_instruction(repair_instruction: RepairInstruction) -> dict[str, object]:
        return {
            "repair_scope": repair_instruction.repair_scope,
            "failure_type": repair_instruction.failure_type,
            "must_fix": _compact_text_list(repair_instruction.must_fix, limit=500),
            "must_preserve": _compact_text_list(repair_instruction.must_preserve, limit=500),
            "must_not_reveal": _compact_text_list(repair_instruction.must_not_reveal, limit=500),
            "scope_reason": _clip_text(repair_instruction.scope_reason, 400),
            "evidence_refs": _compact_text_list(
                repair_instruction.evidence_refs,
                limit=180,
                keep_excerpt=False,
            ),
            "required_delta_patch": _compact_patch(repair_instruction.required_delta_patch),
            "required_belief_patch": _compact_patch(repair_instruction.required_belief_patch),
            "required_hint_patch": _compact_patch(repair_instruction.required_hint_patch),
            "required_payoff_patch": _compact_patch(repair_instruction.required_payoff_patch),
            "design_patch": _compact_patch(repair_instruction.design_patch),
        }

    @staticmethod
    def _compact_review(review: ReviewVerdict) -> dict[str, object]:
        issues = [RepairVerifier._compact_issue(issue) for issue in review.issues[:_MAX_REVIEW_ISSUES_FOR_LLM]]
        if len(review.issues) > _MAX_REVIEW_ISSUES_FOR_LLM:
            issues.append({"omitted_issue_count": len(review.issues) - _MAX_REVIEW_ISSUES_FOR_LLM})
        return {
            "verdict": review.verdict,
            "recommended_action": _clip_text(review.recommended_action, 120),
            "review_summary": _clip_text(review.review_summary, 800),
            "reviewer_mode": _clip_text(review.reviewer_mode, 120),
            "issues": issues,
            "residual_review_issues": [
                RepairVerifier._compact_issue(issue)
                for issue in review.residual_review_issues[:_MAX_REVIEW_ISSUES_FOR_LLM]
            ],
            "evidence_refs": _compact_text_list(
                review.evidence_refs,
                limit=180,
                keep_excerpt=False,
            ),
        }

    @staticmethod
    def _compact_issue(issue) -> dict[str, object]:
        return {
            "rule_name": _clip_text(getattr(issue, "rule_name", ""), 120),
            "severity": _clip_text(getattr(issue, "severity", ""), 40),
            "description": _clip_text(getattr(issue, "description", ""), 700),
            "entity_names": _compact_text_list(
                getattr(issue, "entity_names", None),
                max_items=8,
                limit=80,
            ),
            "issue_type": _clip_text(getattr(issue, "issue_type", ""), 120),
            "target_scope": _clip_text(getattr(issue, "target_scope", ""), 120),
            "issue_group": _clip_text(getattr(issue, "issue_group", ""), 120),
            "evidence_refs": _compact_text_list(
                getattr(issue, "evidence_refs", None),
                limit=180,
                keep_excerpt=False,
            ),
            "suggested_fix": _clip_text(
                getattr(issue, "suggested_fix", ""),
                500,
                keep_excerpt=False,
            ),
            "blocking": bool(getattr(issue, "blocking", False)),
        }

    @staticmethod
    def _compact_writer_output(output: WriterOutput) -> dict[str, object]:
        body = str(output.body or "")
        return {
            "chapter_number": output.chapter_number,
            "title": _clip_text(output.title, 180),
            "char_count": int(output.char_count or len(body)),
            "end_of_chapter_summary": _clip_text(output.end_of_chapter_summary, 700),
            "body_head": _clip_text(body[:1400], 1400),
            "body_tail": _clip_text(body[-1400:] if len(body) > 1400 else "", 1400),
            "structured_counts": {
                "scene_outputs": len(output.scene_outputs),
                "state_changes": len(output.state_changes),
                "new_events": len(output.new_events),
                "thread_beats": len(output.thread_beats),
                "lore_candidates": len(output.lore_candidates),
                "timeline_hints": len(output.timeline_hints),
                "writer_notes": len(output.writer_notes),
                "world_deltas": len(output.world_deltas),
                "reveal_events": len(output.reveal_events),
                "reader_experience_deltas": len(output.reader_experience_deltas),
            },
            "state_changes": [
                {
                    "entity_name": _clip_text(getattr(item, "entity_name", ""), 100),
                    "field": _clip_text(getattr(item, "field", ""), 100),
                    "new_value": _clip_text(getattr(item, "new_value", ""), 180),
                }
                for item in output.state_changes[:6]
            ],
            "thread_beats": [
                {
                    "thread_name": _clip_text(getattr(item, "thread_name", ""), 100),
                    "beat_type": _clip_text(getattr(item, "beat_type", ""), 80),
                    "description": _clip_text(getattr(item, "description", ""), 240),
                }
                for item in output.thread_beats[:6]
            ],
        }
