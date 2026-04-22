from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forwin.protocol.review import normalize_repair_scope


RepairDecisionKind = Literal["pause_for_review", "repair", "final_force_accept_gate"]
REPAIR_SCOPE_SEQUENCE: tuple[str, str, str] = ("draft", "chapter_plan", "band_plan")


@dataclass(slots=True)
class RepairDecision:
    kind: RepairDecisionKind
    scope: str = ""
    attempt_no: int = 0
    max_attempts: int = 0
    reason: str = ""


class RepairPolicy:
    def __init__(self, *, max_attempts: int = 3) -> None:
        self.max_attempts = max(1, min(len(REPAIR_SCOPE_SEQUENCE), int(max_attempts or 3)))

    def decide(
        self,
        *,
        verdict: str,
        operation_mode: str,
        attempts_completed: int,
        requested_scope: str = "",
    ) -> RepairDecision:
        if str(verdict or "") != "fail":
            return RepairDecision(kind="pause_for_review", reason="review-not-fail")
        if str(operation_mode or "") != "blackbox":
            return RepairDecision(kind="pause_for_review", reason="manual-mode")
        if attempts_completed >= self.max_attempts:
            return RepairDecision(
                kind="final_force_accept_gate",
                attempt_no=self.max_attempts,
                max_attempts=self.max_attempts,
                reason="repair-attempts-exhausted",
            )
        default_scope = REPAIR_SCOPE_SEQUENCE[min(attempts_completed, len(REPAIR_SCOPE_SEQUENCE) - 1)]
        normalized_scope = normalize_repair_scope(requested_scope, default=default_scope)
        scope = default_scope if normalized_scope not in REPAIR_SCOPE_SEQUENCE else default_scope
        return RepairDecision(
            kind="repair",
            scope=scope,
            attempt_no=attempts_completed + 1,
            max_attempts=self.max_attempts,
            reason="blackbox-auto-repair",
        )
