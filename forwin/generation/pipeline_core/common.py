"""Writing pipeline – the Phase 0.5 closed-loop pipeline.

Flow per run:
  1. Create project
  2. Plan arc (1 LLM call)
  3. Seed DB with initial state from arc plan
  4. For each chapter:
     a. Assemble context
     b. Write chapter (1 LLM call)
     c. Continuity check (rule-based)
     d. Save draft + review
     e. Update canon state
"""

from __future__ import annotations

import json
import logging
from typing import Any


from forwin.governance import (
    BandCheckpointIssueInfo,
    issue_group_for_issue,
)
from forwin.models.project import ChapterPlan
from forwin.protocol.review import ReviewVerdict
from forwin.planning.future_plan_audit import FuturePlanAuditRun
from forwin.narrative_obligations.types import NarrativeObligation

logger = logging.getLogger(__name__)


def _chapter_plan_prompt_text(plan: ChapterPlan | None) -> str:
    if plan is None:
        return ""
    goals = _loads_json_list(str(plan.goals_json or "[]"))
    contract = _loads_json_list(str(plan.task_contract_json or "[]"))
    return "\n".join(
        part
        for part in (
            str(plan.title or ""),
            str(plan.one_line or ""),
            json.dumps(goals, ensure_ascii=False),
            json.dumps(contract, ensure_ascii=False),
        )
        if part and part != "[]"
    )


def _loads_json_list(raw: str) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _obligation_prompt_item(
    obligation: NarrativeObligation, *, current_chapter: int
) -> dict[str, Any]:
    due_now = bool(obligation.must_resolve_now) or (
        obligation.status == "active"
        and int(obligation.deadline_chapter or 0) <= int(current_chapter or 0)
    )
    status = "open"
    if obligation.status == "resolved":
        status = "fulfilled"
    elif obligation.status == "expired":
        status = "failed"
    elif obligation.status == "waived":
        status = "deferred"
    elif due_now:
        status = "due_now"
    return {
        "obligation_id": obligation.id,
        "description": obligation.summary,
        "holder": ",".join(str(item) for item in obligation.subject_refs)
        or obligation.created_by,
        "target": obligation.payoff_test,
        "status": status,
        "must_address_in_current_output": due_now,
        "failure_condition": obligation.deadline_policy or obligation.blocking_policy,
        "source": ",".join(str(item) for item in obligation.evidence_refs),
    }


def _band_checkpoint_issue_hint(issue: BandCheckpointIssueInfo) -> dict[str, str]:
    return {
        "hint_type": str(issue.code or "band_checkpoint_issue"),
        "message": str(issue.description or ""),
        "matched_text": str(issue.detail or ""),
        "severity": str(issue.severity or ""),
    }


def _band_prompt_result_to_checkpoint_issues(
    result: dict[str, Any],
    *,
    min_blocking_confidence: float,
) -> list[BandCheckpointIssueInfo]:
    output: list[BandCheckpointIssueInfo] = []
    analyzer = str(result.get("analyzer") or "BandCheckpointPromptEvaluator")
    for issue in result.get("issues", []) or []:
        if not isinstance(issue, dict):
            continue
        can_block = str(issue.get("severity") or "").lower() in {
            "critical",
            "blocker",
            "error",
        } and float(issue.get("confidence") or 0.0) >= float(
            min_blocking_confidence or 0.0
        )
        evidence = (
            issue.get("evidence") if isinstance(issue.get("evidence"), list) else []
        )
        detail = "; ".join(
            str(item.get("location") or item.get("quote") or "")
            for item in evidence
            if isinstance(item, dict)
            and str(item.get("location") or item.get("quote") or "").strip()
        )
        output.append(
            BandCheckpointIssueInfo(
                code=str(issue.get("type") or "band_prompt_issue"),
                severity="error" if can_block else "warning",
                issue_group=issue_group_for_issue(
                    code=str(issue.get("type") or "band_prompt_issue")
                ),
                description=str(
                    issue.get("claim")
                    or issue.get("reasoning_summary")
                    or result.get("summary")
                    or ""
                ),
                detail=detail
                or f"{analyzer}:{issue.get('issue_id') or issue.get('type') or 'issue'}",
            )
        )
    return output


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _priority_for_deferred_issue(issue_type: str) -> str:
    normalized = str(issue_type or "").strip()
    if normalized in {"style_repetition_pressure"}:
        return "P3"
    if normalized in {"foreshadowing_payoff", "transition_bridge_needed"}:
        return "P2"
    return "P1"


def _summary_for_deferred_issue(
    *, verdict: ReviewVerdict, issue_type: str, outcome_reason: str
) -> str:
    for issue in verdict.issues:
        if (
            str(
                getattr(issue, "issue_type", "")
                or getattr(issue, "rule_name", "")
                or ""
            )
            == issue_type
        ):
            return str(
                getattr(issue, "description", "") or outcome_reason or issue_type
            )
    return str(outcome_reason or issue_type)


def _payoff_test_for_deferred_issue(
    *,
    verdict: ReviewVerdict,
    issue_type: str,
    deadline_chapter: int,
    summary: str,
) -> str:
    for issue in verdict.issues:
        if (
            str(
                getattr(issue, "issue_type", "")
                or getattr(issue, "rule_name", "")
                or ""
            )
            != issue_type
        ):
            continue
        suggested = str(getattr(issue, "suggested_fix", "") or "").strip()
        if suggested:
            return suggested
    return f"第{int(deadline_chapter or 0)}章前必须偿还：{summary}"


def _future_plan_audit_checkpoint_payload(
    result: FuturePlanAuditRun | None,
) -> dict[str, Any]:
    if result is None:
        return {
            "status": "not_run",
            "inspected_chapters": [],
            "issue_count": 0,
            "issue_types": [],
            "applied_plan_patch_ids": [],
            "blocking_reasons": [],
        }
    return {
        "run_id": result.id,
        "status": result.status,
        "inspected_chapters": list(result.inspected_chapters),
        "issue_count": len(result.issues),
        "issue_types": [issue.issue_type for issue in result.issues],
        "applied_plan_patch_ids": list(result.applied_plan_patch_ids),
        "blocking_reasons": list(result.blocking_reasons),
    }


class TransientLLMChapterFailure(RuntimeError):
    """Current chapter failed because the upstream LLM looked temporarily unavailable."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


__all__ = [name for name in globals() if not name.startswith("__")]
