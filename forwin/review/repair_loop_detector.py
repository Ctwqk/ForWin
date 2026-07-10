from __future__ import annotations

from pydantic import BaseModel, Field

from .repair_scope_router import RoutedSignal


class RepairAttemptRecord(BaseModel):
    attempt_no: int = 0
    scope: str = ""
    signals: list[RoutedSignal] = Field(default_factory=list)
    result_verdict: str = ""


__all__ = ["RepairAttemptRecord"]
