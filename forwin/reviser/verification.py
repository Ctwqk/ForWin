from __future__ import annotations

import json

from forwin.protocol.review import RepairInstruction, RepairVerification, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.utils import parse_llm_json


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
    def _issue_signature(issue) -> tuple[str, str, str]:
        return (
            str(getattr(issue, "rule_name", "") or "").strip(),
            str(getattr(issue, "issue_type", "") or "").strip(),
            str(getattr(issue, "target_scope", "") or "").strip(),
        )

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
        unfixed = [
            str(before_errors[key].description or before_errors[key].rule_name or "").strip()
            for key in before_errors
            if key in after_errors
        ]
        new_risks = [
            str(after_errors[key].description or after_errors[key].rule_name or "").strip()
            for key in after_errors
            if key not in before_errors
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
            normalized = str(item or "").strip()
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
            raw = self.llm_client.chat(
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
                        "content": (
                            "只返回 JSON 对象，字段必须包含："
                            "fixed_all_must_fix、preserved_all_must_preserve、"
                            "unfixed、broken_preserve_constraints、new_risks。\n"
                            f"repair_instruction={json.dumps(repair_instruction.model_dump(mode='json'), ensure_ascii=False)}\n"
                            f"before_review={json.dumps(before_review.model_dump(mode='json'), ensure_ascii=False)}\n"
                            f"after_review={json.dumps(after_review.model_dump(mode='json'), ensure_ascii=False)}\n"
                            f"original_draft={json.dumps(original_output.model_dump(mode='json'), ensure_ascii=False)}\n"
                            f"repaired_draft={json.dumps(repaired_output.model_dump(mode='json'), ensure_ascii=False)}"
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=1600,
                timeout_seconds=30,
                retry_on_timeout=False,
            )
            payload = parse_llm_json(raw, error_prefix="Repair verification")
            verified = RepairVerification.model_validate(payload)
            return verified.model_copy(update={"verifier_mode": "llm_only"})
        except Exception:
            return None
