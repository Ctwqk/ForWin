from __future__ import annotations

from typing import Literal

from .signals import CanonAdmissionGateResult, CanonQualitySignal


GateMode = Literal["off", "shadow", "strict"]


def normalize_gate_mode(value: str | None, *, default: GateMode = "strict") -> GateMode:
    normalized = str(value or default).strip().lower()
    if normalized in {"off", "shadow", "strict"}:
        return normalized  # type: ignore[return-value]
    return default


def evaluate_canon_admission(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    review_id: str = "",
    review_verdict: str = "pass",
    signals: list[CanonQualitySignal] | None = None,
    mode: str = "strict",
    open_terminal_obligation_count: int = 0,
) -> CanonAdmissionGateResult:
    resolved_mode = normalize_gate_mode(mode)
    quality_signals = list(signals or [])
    blocking = [
        signal
        for signal in quality_signals
        if signal.status == "open" and signal.severity == "error"
    ]
    warnings = [
        signal
        for signal in quality_signals
        if signal.status == "open" and signal.severity == "warning"
    ]
    deterministic_refs = [signal.signal_id for signal in blocking]
    if resolved_mode == "off":
        commit_allowed = True
        verdict = _review_verdict_to_gate_verdict(review_verdict)
        summary = "canon quality gate off"
    elif resolved_mode == "shadow":
        commit_allowed = True
        verdict = "warn" if blocking or warnings or open_terminal_obligation_count else _review_verdict_to_gate_verdict(review_verdict)
        summary = (
            f"canon quality gate shadow: blocking={len(blocking)}, "
            f"warnings={len(warnings)}, open_obligations={open_terminal_obligation_count}"
        )
    else:
        commit_allowed = not blocking and open_terminal_obligation_count <= 0
        verdict = "fail" if not commit_allowed else (
            "warn" if warnings or str(review_verdict) == "warn" else "pass"
        )
        summary = (
            f"canon quality gate strict: commit_allowed={commit_allowed}, "
            f"blocking={len(blocking)}, warnings={len(warnings)}, "
            f"open_obligations={open_terminal_obligation_count}"
        )

    return CanonAdmissionGateResult(
        project_id=project_id,
        chapter_number=int(chapter_number or 0),
        draft_id=draft_id,
        review_id=review_id,
        commit_allowed=commit_allowed,
        verdict=verdict,
        blocking_issue_count=len(blocking),
        warning_issue_count=len(warnings),
        open_terminal_obligation_count=max(0, int(open_terminal_obligation_count or 0)),
        deterministic_issue_refs=deterministic_refs,
        required_repair_scope="draft" if blocking else None,
        gate_summary=summary,
    )


def _review_verdict_to_gate_verdict(value: str) -> Literal["pass", "warn", "fail"]:
    normalized = str(value or "pass").strip().lower()
    if normalized in {"pass", "warn", "fail"}:
        return normalized  # type: ignore[return-value]
    return "pass"
