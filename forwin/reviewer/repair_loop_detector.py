from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .repair_scope_router import RepairScopeKind, RoutedSignal


class RepairAttemptRecord(BaseModel):
    attempt_no: int = 0
    scope: str = ""
    signals: list[RoutedSignal] = Field(default_factory=list)
    result_verdict: str = ""


@dataclass(slots=True)
class RepairLoopResult:
    loop_detected: bool = False
    route_scope: str = ""
    similarity: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class RepairLoopDetector:
    similarity_threshold: float = 0.7

    def detect(
        self,
        *,
        scope: str,
        signals: list[RoutedSignal],
        history: list[RepairAttemptRecord],
    ) -> RepairLoopResult:
        current = _fingerprints(signals)
        if not current:
            return RepairLoopResult(loop_detected=False)
        for record in history:
            if str(record.scope or "") != str(scope or ""):
                continue
            if str(record.result_verdict or "") != "fail":
                continue
            previous = _fingerprints(record.signals)
            similarity = _jaccard(current, previous)
            if similarity >= float(self.similarity_threshold):
                return RepairLoopResult(
                    loop_detected=True,
                    route_scope=RepairScopeKind.OPERATOR.value,
                    similarity=similarity,
                    reason=f"repeated repair scope {scope} with overlapping signals",
                )
        return RepairLoopResult(loop_detected=False)


def attempt_record_from_history_item(item: dict[str, Any]) -> RepairAttemptRecord:
    raw_signals = item.get("signals") or item.get("routed_signals") or []
    signals: list[RoutedSignal] = []
    if isinstance(raw_signals, list):
        for raw in raw_signals:
            if isinstance(raw, RoutedSignal):
                signals.append(raw)
            elif isinstance(raw, dict):
                signals.append(
                    RoutedSignal(
                        kind=str(raw.get("kind") or ""),
                        severity=str(raw.get("severity") or "warning"),
                        subject_key=str(raw.get("subject_key") or ""),
                        description=str(raw.get("description") or ""),
                        source_signal_id=str(raw.get("source_signal_id") or ""),
                        source=str(raw.get("source") or ""),
                        payload=raw.get("payload") if isinstance(raw.get("payload"), dict) else {},
                    )
                )
    return RepairAttemptRecord(
        attempt_no=int(item.get("attempt_no") or 0),
        scope=str(item.get("repair_scope") or item.get("scope") or ""),
        signals=signals,
        result_verdict=str(item.get("result_verdict") or ""),
    )


def _fingerprints(signals: list[RoutedSignal]) -> set[tuple[str, str]]:
    return {
        (str(signal.kind or "").strip(), str(signal.subject_key or "").strip())
        for signal in signals
        if str(signal.kind or "").strip()
    }


def _jaccard(left: set[tuple[str, str]], right: set[tuple[str, str]]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


__all__ = [
    "RepairAttemptRecord",
    "RepairLoopDetector",
    "RepairLoopResult",
    "attempt_record_from_history_item",
]
