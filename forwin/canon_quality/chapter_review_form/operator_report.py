from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forwin.reviewer.repair_loop_detector import RepairAttemptRecord
from forwin.reviewer.repair_scope_router import RoutedSignal


class OperatorReport(BaseModel):
    project_id: str
    chapter_number: int
    latest_signals: list[dict[str, Any]] = Field(default_factory=list)
    repair_history: list[dict[str, Any]] = Field(default_factory=list)
    suspected_root_cause: str = ""
    suggested_actions: list[str] = Field(default_factory=list)
    artifact_links: dict[str, str] = Field(default_factory=dict)


def build_report(
    *,
    project_id: str,
    chapter_number: int,
    latest_signals: list[RoutedSignal],
    repair_history: list[RepairAttemptRecord],
    artifact_links: dict[str, str] | None = None,
) -> OperatorReport:
    root_cause = _suspected_root_cause(latest_signals)
    return OperatorReport(
        project_id=project_id,
        chapter_number=int(chapter_number or 0),
        latest_signals=[signal.__dict__ for signal in latest_signals],
        repair_history=[record.model_dump(mode="json") for record in repair_history],
        suspected_root_cause=root_cause,
        suggested_actions=_suggested_actions(root_cause, latest_signals),
        artifact_links=dict(artifact_links or {}),
    )


def _suspected_root_cause(signals: list[RoutedSignal]) -> str:
    kinds = {str(signal.kind or "") for signal in signals}
    if any(kind in kinds for kind in {"form_schema_invalid", "writer_prompt_assembly_error"}):
        return "infrastructure"
    if any(kind.startswith("subworld_admission_") for kind in kinds):
        return "subworld_admission"
    if any("countdown" in kind or "active_rule" in kind for kind in kinds):
        return "active_rules"
    if kinds:
        return "writer_repair"
    return "unknown"


def _suggested_actions(root_cause: str, signals: list[RoutedSignal]) -> list[str]:
    if root_cause == "infrastructure":
        return ["inspect form schema/coercion error before invoking writer repair"]
    if root_cause == "subworld_admission":
        return ["admit known canon entity to chapter subworld roster or register new entity explicitly"]
    if root_cause == "active_rules":
        return ["verify trigger quote and register or revoke the active rule in canon state"]
    return [f"inspect signal {signals[0].kind} and choose the correct repair layer"] if signals else ["inspect repair history"]


__all__ = ["OperatorReport", "build_report"]
