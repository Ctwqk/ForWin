from __future__ import annotations

import json

from forwin.protocol.review import (
    ContinuityIssue,
    RepairInstruction,
    RepairVerification,
    ReviewVerdict,
)
from forwin.review.issue_groups import issue_group_for_issue


def review_event_payload(review: ReviewVerdict) -> dict[str, object]:
    return {
        "verdict": review.verdict,
        "issue_types": [
            str(getattr(issue, "issue_type", getattr(issue, "rule_name", "")) or "")
            for issue in review.issues
        ],
        "issue_groups": [
            str(
                getattr(issue, "issue_group", "")
                or issue_group_for_issue(
                    issue_type=str(getattr(issue, "issue_type", "") or ""),
                    rule_name=str(getattr(issue, "rule_name", "") or ""),
                )
            )
            for issue in review.issues
        ],
        "forced_accept_applied": bool(review.forced_accept_applied),
    }


def review_issue_payloads(review: ReviewVerdict) -> list[dict[str, object]]:
    issues = review.residual_review_issues or review.issues
    return [issue.model_dump(mode="json") for issue in issues]


def review_canon_risk(review: ReviewVerdict) -> str:
    if review.final_residual_decision is not None:
        return str(review.final_residual_decision.canon_risk or "")
    if review.forced_accept_applied:
        return "low"
    if review.verdict == "fail":
        return "high"
    return ""


def load_json_list(raw: str) -> list[object]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []
    return payload if isinstance(payload, list) else []


def repair_verification_issue(
    *,
    rule_name: str,
    description: str,
    suggested_fix: str,
) -> ContinuityIssue:
    return ContinuityIssue(
        rule_name=rule_name,
        severity="error",
        description=description,
        reviewer="repair_verifier",
        issue_type="repair_verification",
        target_scope="chapter",
        evidence_refs=[],
        suggested_fix=suggested_fix,
    )


def repair_policy_requested_scope(review: ReviewVerdict) -> str:
    instruction = getattr(review, "repair_instruction", None)
    if instruction is None:
        return ""
    requested_scope = str(getattr(instruction, "repair_scope", "") or "").strip()
    if review_has_structural_repair_issue(review):
        return ""
    return requested_scope


def review_has_structural_repair_issue(review: ReviewVerdict) -> bool:
    structural_issue_types = {
        "countdown_non_monotonic",
        "artifact_count_explanation",
        "artifact_ledger_conflict",
        "identity_conflict",
        "identity_ambiguity",
        "payoff_miss",
        "unpaid_promise_debt",
        "world_model_conflict",
        "cognition_conflict",
    }
    structural_target_scopes = {
        "ledger",
        "character",
        "band",
        "arc",
        "book",
        "world_model",
    }
    for issue in getattr(review, "issues", []) or []:
        issue_type = str(getattr(issue, "issue_type", "") or "").strip()
        target_scope = str(getattr(issue, "target_scope", "") or "").strip()
        if (
            issue_type in structural_issue_types
            or target_scope in structural_target_scopes
        ):
            return True
    return False


def merge_repair_verification(
    review: ReviewVerdict,
    verification: RepairVerification,
    repair_instruction: RepairInstruction | None,
) -> ReviewVerdict:
    merged_review = review.model_copy(update={"repair_verification": verification})
    if (
        verification.fixed_all_must_fix is not False
        and verification.preserved_all_must_preserve is not False
    ):
        return merged_review

    issues = list(merged_review.issues)
    for item in verification.unfixed:
        issues.append(
            repair_verification_issue(
                rule_name="repair_unfixed",
                description=f"repair 未真正修复：{item}",
                suggested_fix="升级 repair scope，并继续针对 must_fix 重写。",
            )
        )
    for item in verification.broken_preserve_constraints:
        issues.append(
            repair_verification_issue(
                rule_name="repair_preserve_breach",
                description=f"repair 破坏了 must_preserve：{item}",
                suggested_fix="保留既有约束后重新修复，不允许以修 A 伤 B。",
            )
        )
    summary_parts = [str(merged_review.review_summary or "").strip()]
    if verification.unfixed:
        summary_parts.append("repair verification: must_fix 仍未完全修复")
    if verification.broken_preserve_constraints:
        summary_parts.append("repair verification: must_preserve 被破坏")
    return merged_review.model_copy(
        update={
            "verdict": "fail",
            "recommended_action": "rewrite",
            "issues": issues,
            "review_summary": " | ".join(part for part in summary_parts if part),
            "repair_instruction": merged_review.repair_instruction
            or repair_instruction,
        }
    )
