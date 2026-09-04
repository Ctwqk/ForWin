from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_post_canon_maintenance import _durable_service
from forwin.maintenance.post_canon import PostCanonMaintenanceError
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.outbox import OutboxEvent
from forwin.models.project import Project
from forwin.outbox.worker import OutboxClaim, run_one_outbox_event
from forwin.storage.artifacts import ArtifactStore, LocalObjectStore


class UnavailableStore(LocalObjectStore):
    unavailable = True

    def write_text(self, relative_path, content, *, content_type):
        if self.unavailable:
            raise OSError("minio unavailable")
        return super().write_text(relative_path, content, content_type=content_type)


@pytest.mark.parametrize("phase", ["world", "order_controls"])
def test_storage_outage_preserves_business_and_replays_only_frozen_trace(
    tmp_path, phase
):
    service, sessions, calls = _durable_service()
    objects = UnavailableStore(str(tmp_path))
    service.artifact_store = ArtifactStore(object_store=objects)
    attempts = []
    service.llm_client = SimpleNamespace(
        drain_llm_attempt_events=lambda: attempts.pop(0) if attempts else []
    )

    def business(session, _commit):
        calls.append("business")
        project = session.get(Project, "project-1")
        project.setting_summary = "business committed"
        attempts.append(
            [
                {
                    "attempt_no": 1,
                    "stage_key": "world_pressure",
                    "api_key": "must-not-persist",
                }
            ]
        )
        return {"business": "done"}

    if phase == "world":
        service._step_runners[phase] = business
    service.run(canon_commit_id="canon-1", worker_id="business-worker")
    service.run_order_controls(
        canon_commit_id="canon-1",
        runner=business if phase == "order_controls" else lambda *_: {},
    )
    with sessions() as session:
        assert session.get(Project, "project-1").setting_summary == "business committed"
        rows = list(session.scalars(select(PostCanonMaintenanceRun)))
        assert all(row.status == "succeeded" and row.attempts == 1 for row in rows)
        event = session.scalars(select(OutboxEvent)).one()
        assert event.status == "pending"
        payload = json.loads(event.payload_json)
        assert "must-not-persist" not in event.payload_json
        assert (
            payload["content_sha256"]
            == hashlib.sha256(payload["content"].encode()).hexdigest()
        )
        assert payload["content_sha256"] in payload["artifact_key"]
    assert service.barrier_blocking_reasons("canon-1") == []
    assert calls.count("business") == 1

    from forwin.maintenance.trace_upload import build_trace_upload_outbox_handlers

    handlers = build_trace_upload_outbox_handlers(
        artifact_store_provider=lambda: ArtifactStore(object_store=objects)
    )
    outcome = run_one_outbox_event(
        session_factory=sessions,
        worker_id="upload-1",
        handlers=handlers,
        base_delay_seconds=0,
    )
    assert not outcome.processed
    assert service.barrier_blocking_reasons("canon-1") == []
    objects.unavailable = False
    outcome = run_one_outbox_event(
        session_factory=sessions,
        worker_id="upload-2",
        handlers=handlers,
        base_delay_seconds=0,
    )
    assert outcome.processed
    with sessions() as session:
        event = session.scalars(select(OutboxEvent)).one()
        assert event.status == "processed" and event.attempts == 2
        assert all(
            row.attempts == 1
            for row in session.scalars(select(PostCanonMaintenanceRun))
        )
    assert calls.count("business") == 1
    target = tmp_path / "projects/project-1/keyed" / payload["artifact_key"]
    assert target.read_text() == payload["content"]


def test_real_business_failure_still_rolls_back_and_blocks():
    service, sessions, _ = _durable_service()

    def business(session, _commit):
        session.get(Project, "project-1").setting_summary = "must roll back"
        raise RuntimeError("world business failed")

    service._step_runners["world"] = business
    with pytest.raises(PostCanonMaintenanceError, match="world business failed"):
        service.run(canon_commit_id="canon-1", worker_id="business-worker")
    with sessions() as session:
        assert session.get(Project, "project-1").setting_summary != "must roll back"
        assert not list(session.scalars(select(OutboxEvent)))
    assert service.barrier_blocking_reasons("canon-1")


def test_order_control_traces_replay_out_of_order_without_overwriting(tmp_path):
    service, sessions, _ = _durable_service()
    service.run(canon_commit_id="canon-1", worker_id="business-worker")
    drains = iter(([], [{"attempt_no": 1}], [], [{"attempt_no": 2}]))
    service.llm_client = SimpleNamespace(drain_llm_attempt_events=lambda: next(drains))
    service.run_order_controls(
        canon_commit_id="canon-1",
        runner=lambda *_: {"blocking_reasons": ["unresolved contract"]},
    )
    service.run_order_controls(canon_commit_id="canon-1", runner=lambda *_: {})
    with sessions() as session:
        events = list(
            session.scalars(
                select(OutboxEvent).order_by(OutboxEvent.created_at, OutboxEvent.id)
            )
        )
        assert len(events) == 2
        before = {
            row.id: row.result_json
            for row in session.scalars(select(PostCanonMaintenanceRun))
        }
    from forwin.maintenance.trace_upload import build_trace_upload_outbox_handlers

    handlers = build_trace_upload_outbox_handlers(
        artifact_store_provider=lambda: ArtifactStore(str(tmp_path))
    )
    keys = []
    for event in reversed(events):
        payload = json.loads(event.payload_json)
        claim = OutboxClaim(
            event.id,
            event.event_id,
            event.event_type,
            event.aggregate_type,
            event.aggregate_id,
            payload,
            "uploader",
            1,
            1,
        )
        handlers[event.event_type](claim)
        handlers[event.event_type](
            claim
        )  # upload acknowledged late / same event replay
        keys.append(payload["artifact_key"])
        assert (
            tmp_path / "projects/project-1/keyed" / payload["artifact_key"]
        ).read_text() == payload["content"]
    assert len(set(keys)) == 2
    with sessions() as session:
        assert before == {
            row.id: row.result_json
            for row in session.scalars(select(PostCanonMaintenanceRun))
        }


@pytest.mark.parametrize(
    "corrupt",
    [
        "content",
        "content_sha256",
        "artifact_key",
        "event_id",
        "project_id",
        "step_name",
    ],
)
def test_trace_handler_rejects_corrupt_identity_before_resolving_store(corrupt):
    service, sessions, _ = _durable_service()
    service.run(canon_commit_id="canon-1", worker_id="business-worker")
    drains = iter(([], [{"attempt_no": 1}]))
    service.llm_client = SimpleNamespace(drain_llm_attempt_events=lambda: next(drains))
    service.run_order_controls(canon_commit_id="canon-1", runner=lambda *_: {})
    with sessions() as session:
        event = session.scalars(select(OutboxEvent)).one()
    payload = json.loads(event.payload_json)
    claim = OutboxClaim(
        event.id,
        event.event_id,
        event.event_type,
        event.aggregate_type,
        event.aggregate_id,
        payload,
        "uploader",
        1,
        1,
    )
    if corrupt == "event_id":
        claim = replace(claim, event_id="wrong")
    else:
        claim = replace(claim, payload={**payload, corrupt: "wrong"})
    from forwin.maintenance.trace_upload import build_trace_upload_outbox_handlers

    def forbidden():
        raise AssertionError("invalid payload must not resolve artifact store")

    handlers = build_trace_upload_outbox_handlers(artifact_store_provider=forbidden)
    with pytest.raises(ValueError):
        handlers[event.event_type](claim)


def test_runtime_trace_handler_never_resolves_business_services(tmp_path):
    from forwin.runtime.container import RuntimeContainer

    service, sessions, _ = _durable_service()
    service.run(canon_commit_id="canon-1", worker_id="business-worker")
    drains = iter(([], [{"attempt_no": 1}]))
    service.llm_client = SimpleNamespace(drain_llm_attempt_events=lambda: next(drains))
    service.run_order_controls(canon_commit_id="canon-1", runner=lambda *_: {})
    with sessions() as session:
        event = session.scalars(select(OutboxEvent)).one()
    payload = json.loads(event.payload_json)

    def forbidden():
        raise AssertionError(
            "trace delivery must not build LLM, planning, memory, or publisher"
        )

    core = SimpleNamespace(
        session_factory=sessions,
        infrastructure=SimpleNamespace(),
        artifact_store=ArtifactStore(str(tmp_path)),
    )
    runtime = SimpleNamespace(
        core_services=lambda: core,
        _provide_outbox_memory_index=forbidden,
        _provide_outbox_post_canon_maintenance=forbidden,
        _provide_outbox_publisher_jobs=forbidden,
    )
    handlers = RuntimeContainer._build_outbox_handlers(runtime)
    result = run_one_outbox_event(
        session_factory=sessions, worker_id="uploader", handlers=handlers
    )
    assert result.processed
    assert (
        tmp_path / "projects/project-1/keyed" / payload["artifact_key"]
    ).read_text() == payload["content"]


def test_upload_acknowledgement_crash_reclaims_same_frozen_payload(
    tmp_path, monkeypatch
):
    from datetime import datetime, timedelta, timezone
    from forwin.maintenance.trace_upload import build_trace_upload_outbox_handlers
    from forwin.outbox import store

    service, sessions, calls = _durable_service()
    service.run(canon_commit_id="canon-1", worker_id="business-worker")
    drains = iter(([], [{"attempt_no": 1}]))
    service.llm_client = SimpleNamespace(drain_llm_attempt_events=lambda: next(drains))
    service.run_order_controls(canon_commit_id="canon-1", runner=lambda *_: {})
    handlers = build_trace_upload_outbox_handlers(
        artifact_store_provider=lambda: ArtifactStore(str(tmp_path))
    )
    original = store.mark_outbox_event_processed

    def fail_ack(*_args, **_kwargs):
        raise RuntimeError("ack database connection lost")

    monkeypatch.setattr(store, "mark_outbox_event_processed", fail_ack)
    with pytest.raises(RuntimeError, match="ack database connection lost"):
        run_one_outbox_event(
            session_factory=sessions, worker_id="upload-1", handlers=handlers
        )
    with sessions.begin() as session:
        row = session.scalars(select(OutboxEvent)).one()
        payload = row.payload_json
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    monkeypatch.setattr(store, "mark_outbox_event_processed", original)
    assert run_one_outbox_event(
        session_factory=sessions, worker_id="upload-2", handlers=handlers
    ).processed
    with sessions() as session:
        row = session.scalars(select(OutboxEvent)).one()
        assert row.payload_json == payload
        assert row.attempts == 2 and row.lease_epoch == 2
    assert calls == ["planning", "arc", "world", "feedback"]
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_failed_completion_rolls_back_business_and_already_enqueued_trace():
    service, sessions, _ = _durable_service()
    pending = []
    service.llm_client = SimpleNamespace(
        drain_llm_attempt_events=lambda: pending.pop(0) if pending else []
    )

    def business(session, _commit):
        session.get(Project, "project-1").setting_summary = "uncommitted"
        pending.append([{"attempt_no": 1}])
        return {}

    service._step_runners["world"] = business
    complete = service._complete_claim_in_session

    def fail_world(session, claim, result):
        if claim.step_name == "world":
            assert list(session.scalars(select(OutboxEvent)))
            raise RuntimeError("completion write failed")
        complete(session, claim, result)

    service._complete_claim_in_session = fail_world
    with pytest.raises(PostCanonMaintenanceError, match="completion write failed"):
        service.run(canon_commit_id="canon-1", worker_id="business-worker")
    with sessions() as session:
        assert not list(session.scalars(select(OutboxEvent)))
        assert session.get(Project, "project-1").setting_summary != "uncommitted"
    assert service.barrier_blocking_reasons("canon-1")
