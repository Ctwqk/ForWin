from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class LongRunMode(StrEnum):
    daily_serial = "daily_serial"
    factory_batch = "factory_batch"
    soak_test = "soak_test"


ResumePolicy = Literal[
    "manual_after_failed_chapter",
    "auto_after_infrastructure_failure",
]


class LongRunPolicy(BaseModel):
    mode: LongRunMode = LongRunMode.daily_serial
    batch_size: int = Field(default=1, ge=1, le=50)
    stop_on_chapter_failure: bool = True
    defer_observation_failures: bool = False
    payoff_gap_limit: int = Field(default=2, ge=1, le=10)
    resume_policy: ResumePolicy = "manual_after_failed_chapter"


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _clamped_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return min(maximum, max(minimum, normalized))


def normalize_long_run_policy(raw: Any) -> LongRunPolicy:
    if isinstance(raw, LongRunPolicy):
        return raw
    payload = raw if isinstance(raw, dict) else {}
    try:
        return LongRunPolicy.model_validate(payload)
    except Exception:
        cleaned = dict(payload)
        mode = str(cleaned.get("mode") or "").strip()
        if mode not in {item.value for item in LongRunMode}:
            cleaned["mode"] = LongRunMode.daily_serial.value
        cleaned["batch_size"] = _clamped_int(
            cleaned.get("batch_size", 1),
            default=1,
            minimum=1,
            maximum=50,
        )
        cleaned["payoff_gap_limit"] = _clamped_int(
            cleaned.get("payoff_gap_limit", 2),
            default=2,
            minimum=1,
            maximum=10,
        )
        if cleaned.get("resume_policy") not in {
            "manual_after_failed_chapter",
            "auto_after_infrastructure_failure",
        }:
            cleaned["resume_policy"] = "manual_after_failed_chapter"
        cleaned["stop_on_chapter_failure"] = _coerce_bool(
            cleaned.get("stop_on_chapter_failure"),
            default=True,
        )
        cleaned["defer_observation_failures"] = _coerce_bool(
            cleaned.get("defer_observation_failures"),
            default=False,
        )
        return LongRunPolicy.model_validate(cleaned)
