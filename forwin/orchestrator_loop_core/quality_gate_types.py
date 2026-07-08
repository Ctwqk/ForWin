from __future__ import annotations

from dataclasses import dataclass

from forwin.canon_quality.signals import CanonAdmissionGateResult


@dataclass(frozen=True)
class CanonQualityGateOutcome:
    blocked_path: str = ""
    gate_result: CanonAdmissionGateResult | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_path)

    def __bool__(self) -> bool:
        return self.blocked


@dataclass(frozen=True)
class CanonApplyOutcome:
    blocked_path: str = ""
    block_kind: str = ""
    canon_gate_result: CanonAdmissionGateResult | None = None

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


__all__ = ["CanonApplyOutcome", "CanonQualityGateOutcome"]
