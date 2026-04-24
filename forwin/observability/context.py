from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperationContext:
    project_id: str = ""
    task_id: str = ""
    arc_id: str = ""
    band_id: str = ""
    chapter_number: int = 0
    stage: str = ""
    actor_type: str = "system"
    actor_id: str = ""
    parent_event_id: str = ""
    causal_root_id: str = ""
    operation_id: str = ""
    test_run_id: str = ""

    def payload_fields(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.arc_id:
            payload["arc_id"] = self.arc_id
        if self.stage:
            payload["stage"] = self.stage
        if self.operation_id:
            payload["operation_id"] = self.operation_id
        if self.test_run_id:
            payload["test_run_id"] = self.test_run_id
        return payload
