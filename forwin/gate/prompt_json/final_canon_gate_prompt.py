from __future__ import annotations

from typing import Any

from forwin.canon_quality.prompt_json.base import PromptJsonAnalyzer
from forwin.canon_quality.prompt_json.schemas import common_output_schema
from forwin.canon_quality.prompt_json.validation import issue_can_block, result_can_block

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Decide whether the writer output should be admitted into canon.

Rules:
- Do not invent new issues beyond the analyzer results unless there is direct evidence in writer_output.
- Warnings should not block canon admission.
- Uncertain results should not block canon admission.
- A result can block only if it has severity critical, confidence above policy threshold, and direct evidence.
- If the correct action is to admit with required patch, return admit_with_patch.
- If the issue is a plan update rather than canon contradiction, prefer admit_with_plan_update.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="FinalCanonGatePromptEvaluator",
    extra_required=["decision", "blocking_issues", "non_blocking_warnings", "required_patches", "human_review_reasons"],
)


class FinalCanonGatePromptEvaluator(PromptJsonAnalyzer):
    name = "FinalCanonGatePromptEvaluator"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA

    def evaluate(self, input_payload: dict[str, Any]) -> dict[str, Any]:
        if self.llm_client is not None:
            return self.analyze(input_payload)
        return evaluate_final_canon_gate(input_payload)


def evaluate_final_canon_gate(input_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(input_payload or {})
    policy = payload.get("canon_admission_policy") if isinstance(payload.get("canon_admission_policy"), dict) else {}
    min_confidence = float(policy.get("min_blocking_confidence", 0.8) or 0.8)
    require_evidence = bool(policy.get("require_evidence_for_block", True))
    analyzer_results = [
        item for item in payload.get("analyzer_results", [])
        if isinstance(item, dict)
    ]
    blocking_issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    required_patches: list[dict[str, str]] = []
    for result in analyzer_results:
        analyzer = str(result.get("analyzer") or "PromptJsonAnalyzer")
        for issue in result.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            issue_id = str(issue.get("issue_id") or "")
            if issue_can_block(issue, min_confidence=min_confidence, require_evidence=require_evidence):
                evidence = issue.get("evidence") if isinstance(issue.get("evidence"), list) else []
                first = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
                blocking_issues.append(
                    {
                        "source_analyzer": analyzer,
                        "issue_id": issue_id,
                        "why_blocking": str(issue.get("claim") or issue.get("type") or ""),
                        "evidence_quote": str(first.get("quote") or ""),
                        "confidence": float(issue.get("confidence") or 0.0),
                    }
                )
                continue
            warnings.append(
                {
                    "source_analyzer": analyzer,
                    "issue_id": issue_id,
                    "reason": str(issue.get("claim") or issue.get("type") or ""),
                }
            )
            if str(issue.get("type") or "") in {"plan_needs_update", "soft_plan_mismatch", "needs_plan_update"}:
                required_patches.append(
                    {
                        "patch_type": "plan_patch",
                        "description": str(issue.get("suggested_fix") or issue.get("claim") or "Update future plan."),
                        "source_issue_id": issue_id,
                    }
                )
    has_block = any(
        result_can_block(
            result,
            min_confidence=min_confidence,
            require_evidence=require_evidence,
        )
        for result in analyzer_results
    ) or bool(blocking_issues)
    if has_block:
        decision = "reject"
    elif required_patches:
        decision = "admit_with_plan_update"
    elif warnings:
        decision = "admit_with_warning"
    else:
        decision = "admit"
    return {
        "analyzer": "FinalCanonGatePromptEvaluator",
        "version": "1.0",
        "decision": decision,
        "blocking": has_block,
        "confidence": 0.9 if analyzer_results else 0.0,
        "summary": f"final canon gate decision: {decision}",
        "blocking_issues": blocking_issues,
        "non_blocking_warnings": warnings,
        "required_patches": required_patches,
        "human_review_reasons": [],
    }
