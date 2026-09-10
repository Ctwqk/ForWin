from __future__ import annotations

from dataclasses import replace

import pytest

from forwin.canon.outbox_events import (
    CANON_PHASE3_REQUESTED,
    CANON_PROJECTION_REQUESTED,
    CANON_PUBLISHER_REQUESTED,
    canon_commit_id,
    canon_event_id,
)
from forwin.knowledge_system import projection_jobs
from forwin.outbox.handlers import (
    _merge_handler_maps,
    build_default_outbox_handlers,
)
from forwin.outbox.worker import OutboxClaim


CANON_KEY = "canon-key-1"
CANON_COMMIT_ID = canon_commit_id(CANON_KEY)


def _claim() -> OutboxClaim:
    return OutboxClaim(
        row_id="row-1",
        event_id=canon_event_id(CANON_KEY, CANON_PROJECTION_REQUESTED),
        event_type=CANON_PROJECTION_REQUESTED,
        aggregate_type="project",
        aggregate_id="project-1",
        payload={
            "schema_version": 1,
            "canon_commit_id": CANON_COMMIT_ID,
            "canon_idempotency_key": CANON_KEY,
            "project_id": "project-1",
            "chapter_number": 1,
            "candidate_id": "candidate-1",
        },
        worker_id="worker-1",
        lease_epoch=1,
        attempts=1,
    )


def _phase3_claim() -> OutboxClaim:
    return OutboxClaim(
        row_id="row-phase3",
        event_id=canon_event_id(CANON_KEY, CANON_PHASE3_REQUESTED),
        event_type=CANON_PHASE3_REQUESTED,
        aggregate_type="project",
        aggregate_id="project-1",
        payload={
            "schema_version": 1,
            "canon_commit_id": CANON_COMMIT_ID,
            "canon_idempotency_key": CANON_KEY,
            "project_id": "project-1",
            "chapter_number": 1,
            "candidate_id": "candidate-1",
        },
        worker_id="worker-1",
        lease_epoch=2,
        attempts=1,
    )


def _publisher_claim() -> OutboxClaim:
    return OutboxClaim(
        row_id="row-publisher",
        event_id=canon_event_id(CANON_KEY, CANON_PUBLISHER_REQUESTED),
        event_type=CANON_PUBLISHER_REQUESTED,
        aggregate_type="project",
        aggregate_id="project-1",
        payload={
            "schema_version": 1,
            "canon_commit_id": CANON_COMMIT_ID,
            "canon_idempotency_key": CANON_KEY,
            "project_id": "project-1",
            "chapter_number": 1,
            "candidate_id": "candidate-1",
            "chapter_title": "第一章",
            "body_sha256": "body-hash-1",
            "publisher_bindings": (
                {
                    "platform": "qidian",
                    "book_name": "事件快照书名",
                    "book_meta": {"theme_tags": ("悬疑", "都市")},
                },
            ),
            "publish": True,
        },
        worker_id="worker-1",
        lease_epoch=3,
        attempts=1,
    )


def test_default_handler_registration_does_not_construct_memory_index(
    monkeypatch,
) -> None:
    calls: list[str] = []
    memory_index = object()

    def provider():
        calls.append("create")
        return memory_index

    monkeypatch.setattr(
        projection_jobs,
        "handle_projection_refresh_outbox_event",
        lambda _event, *, memory_index_provider, **_kwargs: calls.append(
            "handle" if memory_index_provider() is memory_index else "missing"
        ),
    )

    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        memory_index_provider=provider,
    )

    assert calls == []
    handlers[CANON_PROJECTION_REQUESTED](_claim())
    handlers[CANON_PROJECTION_REQUESTED](_claim())
    assert calls == ["create", "handle", "handle"]


def test_memory_index_provider_failure_is_visible_and_retryable(monkeypatch) -> None:
    calls: list[str] = []
    memory_index = object()

    def provider():
        calls.append("provider")
        if calls.count("provider") == 1:
            raise OSError("qdrant unavailable")
        return memory_index

    monkeypatch.setattr(
        projection_jobs,
        "handle_projection_refresh_outbox_event",
        lambda _event, *, memory_index_provider, **_kwargs: calls.append(
            "handle" if memory_index_provider() is memory_index else "missing"
        ),
    )
    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        memory_index_provider=provider,
    )

    with pytest.raises(OSError, match="qdrant unavailable"):
        handlers[CANON_PROJECTION_REQUESTED](_claim())
    handlers[CANON_PROJECTION_REQUESTED](_claim())
    handlers[CANON_PROJECTION_REQUESTED](_claim())

    assert calls == ["provider", "provider", "handle", "handle"]


def test_empty_memory_index_provider_result_is_not_cached(monkeypatch) -> None:
    calls: list[str] = []
    memory_index = object()

    def provider():
        calls.append("provider")
        return None if calls.count("provider") == 1 else memory_index

    monkeypatch.setattr(
        projection_jobs,
        "handle_projection_refresh_outbox_event",
        lambda _event, *, memory_index_provider, **_kwargs: (
            memory_index_provider(),
            calls.append("handle"),
        ),
    )
    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        memory_index_provider=provider,
    )

    with pytest.raises(RuntimeError, match="returned no resource"):
        handlers[CANON_PROJECTION_REQUESTED](_claim())
    handlers[CANON_PROJECTION_REQUESTED](_claim())

    assert calls == ["provider", "provider", "handle"]


def test_phase3_handler_resolves_service_lazily_once() -> None:
    calls: list[object] = []

    class Service:
        def resolve_event_canon_commit(self, **identity):
            calls.append(("resolve", identity))
            return CANON_COMMIT_ID

        def run(self, **request) -> None:
            calls.append(("run", request))

    service = Service()

    def provider():
        calls.append("provider")
        return service

    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        post_canon_service_provider=provider,
    )

    assert calls == []
    handlers[CANON_PHASE3_REQUESTED](_phase3_claim())
    handlers[CANON_PHASE3_REQUESTED](_phase3_claim())

    assert calls[0] == "provider"
    assert calls.count("provider") == 1
    assert [item[0] for item in calls[1:]] == ["resolve", "run", "resolve", "run"]


def test_publisher_handler_resolves_materializer_lazily_once() -> None:
    calls: list[object] = []

    class Service:
        def materialize_event(self, event) -> None:
            calls.append(("materialize_event", event.model_dump(mode="json")))

    service = Service()

    def provider():
        calls.append("provider")
        return service

    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        publisher_job_service_provider=provider,
    )

    assert calls == []
    handlers[CANON_PUBLISHER_REQUESTED](_publisher_claim())
    handlers[CANON_PUBLISHER_REQUESTED](_publisher_claim())

    assert calls[0] == "provider"
    assert calls.count("provider") == 1
    assert [item[0] for item in calls[1:]] == ["materialize_event", "materialize_event"]
    request = calls[1][1]
    assert request["canon_idempotency_key"] == CANON_KEY
    assert request["publisher_bindings"][0]["platform"] == "qidian"
    assert request["publisher_bindings"][0]["book_name"] == "事件快照书名"
    assert request["publisher_bindings"][0]["book_meta"]["theme_tags"] == ["悬疑", "都市"]


@pytest.mark.parametrize(
    ("event_type", "claim_factory"),
    (
        (CANON_PHASE3_REQUESTED, _phase3_claim),
        (CANON_PUBLISHER_REQUESTED, _publisher_claim),
    ),
)
def test_invalid_canon_event_fails_before_service_resolution(
    event_type,
    claim_factory,
) -> None:
    calls: list[str] = []

    def provider():
        calls.append("provider")
        return object()

    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        post_canon_service_provider=provider,
        publisher_job_service_provider=provider,
    )
    claim = claim_factory()
    invalid = replace(
        claim,
        payload={
            key: value
            for key, value in claim.payload.items()
            if key != "candidate_id"
        },
    )

    with pytest.raises(ValueError, match="candidate_id"):
        handlers[event_type](invalid)

    assert calls == []


def test_nondeterministic_canon_commit_id_fails_before_service_resolution() -> None:
    calls: list[str] = []

    def provider():
        calls.append("provider")
        return object()

    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        post_canon_service_provider=provider,
    )
    claim = _phase3_claim()
    invalid = replace(
        claim,
        payload={**claim.payload, "canon_commit_id": "wrong-commit-id"},
    )

    with pytest.raises(ValueError, match="commit ID is not deterministic"):
        handlers[CANON_PHASE3_REQUESTED](invalid)

    assert calls == []


def test_handler_registry_rejects_duplicate_event_owners() -> None:
    def handler(_event) -> None:
        return None

    with pytest.raises(ValueError, match="duplicate outbox handler owner: event.a"):
        _merge_handler_maps({"event.a": handler}, {"event.a": handler})


def test_default_registry_has_exactly_one_handler_for_each_canon_event() -> None:
    class Phase3Service:
        pass

    class PublisherService:
        pass

    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        post_canon_service_provider=Phase3Service,
        publisher_job_service_provider=PublisherService,
    )

    canon_event_types = {
        CANON_PROJECTION_REQUESTED,
        CANON_PHASE3_REQUESTED,
        CANON_PUBLISHER_REQUESTED,
    }
    assert {key for key in handlers if key.startswith("canon.")} == canon_event_types
