from __future__ import annotations

from forwin.canon_quality.signals import CanonQualitySignal


def dedupe_quality_signals(signals: list[CanonQualitySignal]) -> list[CanonQualitySignal]:
    seen: set[str] = set()
    deduped: list[CanonQualitySignal] = []
    for signal in signals:
        if signal.signal_id in seen:
            continue
        seen.add(signal.signal_id)
        deduped.append(signal)
    return deduped


__all__ = ["dedupe_quality_signals"]
