from __future__ import annotations

from .signals import (
    CanonAdmissionGateResult,
    CanonQualitySignal,
    CharacterStateTransition,
    CountdownLedgerEntry,
)

__all__ = [
    "CanonAdmissionGateResult",
    "CanonQualitySignal",
    "CharacterStateTransition",
    "CountdownLedgerEntry",
    "ObligationResolutionVerifier",
    "evaluate_canon_admission",
]


def __getattr__(name: str):
    if name == "evaluate_canon_admission":
        from .gate import evaluate_canon_admission

        return evaluate_canon_admission
    if name == "ObligationResolutionVerifier":
        from .obligation_verifier import ObligationResolutionVerifier

        return ObligationResolutionVerifier
    raise AttributeError(name)
