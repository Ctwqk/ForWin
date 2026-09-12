from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from forwin.canon_quality.signals import CanonAdmissionGateResult
from forwin.narrative_obligations.resolution_evidence import ObligationResolutionPlan

if TYPE_CHECKING:
    from forwin.canon.plan import CanonCommitPlan
    from forwin.protocol.book_state import BookStateCompileResult


@dataclass(frozen=True)
class CanonQualityGateOutcome:
    blocked_path: str = ""
    gate_result: CanonAdmissionGateResult | None = None
    quality_admission_run_id: str = ""
    obligation_resolution_plan: ObligationResolutionPlan | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_path)

    def __bool__(self) -> bool:
        return self.blocked


@dataclass(frozen=True)
class CanonPreparationOutcome:
    plan: CanonCommitPlan | None = None
    blocked_path: str = ""
    block_kind: str = ""
    canon_gate_result: CanonAdmissionGateResult | None = None

    @property
    def blocked(self) -> bool:
        return self.plan is None

    def __bool__(self) -> bool:
        return self.blocked


@dataclass(frozen=True)
class CanonAdmissionOutcome:
    blocked_path: str = ""
    block_kind: str = ""
    canon_gate_result: CanonAdmissionGateResult | None = None
    commit_id: str = ""
    idempotent: bool = False
    stale: bool = False
    failure_reason: str = ""
    compile_result: BookStateCompileResult | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_path or self.block_kind)

    def __bool__(self) -> bool:
        return self.blocked

    @property
    def repairable_scope(self) -> str:
        if self.block_kind != "canon_quality" or self.canon_gate_result is None:
            return ""
        return str(self.canon_gate_result.required_repair_scope or "")


@dataclass(frozen=True)
class CanonWorldEditOutcome:
    compile_result: BookStateCompileResult
    outbox_event_id: str
