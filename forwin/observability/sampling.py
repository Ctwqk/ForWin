from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpanSampler:
    enabled: bool = True
    performance_enabled: bool = True
    sample_rate: float = 1.0
    slow_span_threshold_ms: int = 1000

    @classmethod
    def from_config(cls, config: object | None) -> "SpanSampler":
        return cls(
            enabled=bool(getattr(config, "observability_enabled", True)),
            performance_enabled=bool(getattr(config, "observability_performance_enabled", True)),
            sample_rate=max(0.0, min(1.0, float(getattr(config, "observability_span_sample_rate", 1.0) or 0.0))),
            slow_span_threshold_ms=max(0, int(getattr(config, "observability_slow_span_threshold_ms", 1000) or 0)),
        )

    def should_start(self) -> bool:
        if not self.enabled or not self.performance_enabled:
            return False
        if self.sample_rate >= 1.0:
            return True
        if self.sample_rate <= 0.0:
            return False
        return random.random() < self.sample_rate
