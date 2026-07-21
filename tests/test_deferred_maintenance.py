from __future__ import annotations

from types import SimpleNamespace

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


def test_post_canon_deferred_event_is_idempotent_for_same_durable_runs() -> None:
    class SessionSpy:
        def __init__(self) -> None:
            self.rows: dict[str, object] = {}

        def get(self, _model, identity):
            return self.rows.get(str(identity))

    class DurableUpdaterSpy(UpdaterSpy):
        def __init__(self) -> None:
            super().__init__()
            self.session = SessionSpy()

        def save_decision_event(self, event) -> None:
            super().save_decision_event(event)
            self.session.rows[event.id] = SimpleNamespace(id=event.id)

    updater = DurableUpdaterSpy()
    record = DeferredMaintenanceRecord(
        project_id="project-1",
        chapter_number=7,
        task_type="post_canon_phase3",
        reason="minio unavailable",
        payload={"canon_commit_id": "canon-1"},
        maintenance_run_ids=["run-world"],
    )

    record_deferred_maintenance(updater, record)
    record_deferred_maintenance(updater, record)

    assert len(updater.events) == 1
    assert updater.events[0]["id"]
