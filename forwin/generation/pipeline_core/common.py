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

from forwin.planning.future_plan_audit import FuturePlanAuditRun
from forwin.writer.execution_errors import (
    TransientLLMChapterFailure as TransientLLMChapterFailure,  # noqa: PLC0414 - public exception alias.
)

logger = logging.getLogger(__name__)


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0








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




__all__ = [name for name in globals() if not name.startswith("__")]
