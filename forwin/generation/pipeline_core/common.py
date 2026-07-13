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

import logging
from typing import Any

from forwin.protocol.review import ReviewVerdict
from forwin.planning.future_plan_audit import FuturePlanAuditRun

logger = logging.getLogger(__name__)


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
