from __future__ import annotations

from forwin.maintenance.deferred import DeferredMaintenanceRecord, record_deferred_maintenance


class UpdaterSpy:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def save_decision_event(self, event) -> None:
        self.events.append(event.model_dump(mode="json"))


def test_record_deferred_maintenance_saves_decision_event() -> None:
    updater = UpdaterSpy()
    record = DeferredMaintenanceRecord(
        project_id="project-1",
        chapter_number=7,
        task_type="memory_index_upsert",
        reason="qdrant timeout",
        payload={"error_class": "TimeoutError"},
    )

    record_deferred_maintenance(updater, record)

    assert updater.events[0]["project_id"] == "project-1"
    assert updater.events[0]["chapter_number"] == 7
    assert updater.events[0]["event_type"] == "deferred_maintenance_recorded"
    assert updater.events[0]["payload"]["task_type"] == "memory_index_upsert"
