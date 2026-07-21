from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.knowledge_system import canon_outbox
from forwin.knowledge_system.canon_outbox import CANON_POST_COMMIT_EVENT
from forwin.maintenance.events import POST_CANON_PHASE3_EVENT
from forwin.outbox.handlers import build_default_outbox_handlers
from forwin.outbox.worker import OutboxClaim


def _claim() -> OutboxClaim:
    return OutboxClaim(
        row_id="row-1",
        event_id="event-1",
        event_type=CANON_POST_COMMIT_EVENT,
        aggregate_type="project",
        aggregate_id="project-1",
        payload={
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
        event_id="event-phase3",
        event_type=POST_CANON_PHASE3_EVENT,
        aggregate_type="project",
        aggregate_id="project-1",
        payload={
            "canon_commit_id": "canon-1",
            "project_id": "project-1",
            "chapter_number": 1,
            "candidate_id": "candidate-1",
        },
        worker_id="worker-1",
        lease_epoch=2,
        attempts=1,
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        retrieval_backend="qdrant",
        retrieval_root="unused",
        qdrant_url="http://qdrant:6333",
        qdrant_collection="chapters",
        llm_kb_qdrant_collection="llm-kb",
        embedding_backend="gateway",
        embedding_base_url="http://embedding:8080",
        embedding_api_key="",
        embedding_model="model",
        embedding_dims=384,
        embedding_required=True,
    )


def test_default_handler_registration_does_not_construct_memory_index(
    monkeypatch,
) -> None:
    calls: list[str] = []
    memory_index = object()
    monkeypatch.setattr(
        canon_outbox,
        "create_memory_index",
        lambda **_kwargs: calls.append("create") or memory_index,
    )
    monkeypatch.setattr(
        canon_outbox,
        "handle_canon_post_commit_outbox_event",
        lambda _event, *, memory_index, **_kwargs: calls.append(
            "handle" if memory_index is not None else "missing"
        ),
    )

    handlers = build_default_outbox_handlers(
        session_factory=lambda: None,
        config=_config(),
    )

    assert calls == []
    handlers[CANON_POST_COMMIT_EVENT](_claim())
    handlers[CANON_POST_COMMIT_EVENT](_claim())
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
        canon_outbox,
        "handle_canon_post_commit_outbox_event",
        lambda _event, *, memory_index, **_kwargs: calls.append(
            "handle" if memory_index is not None else "missing"
        ),
    )
    handlers = canon_outbox.build_canon_outbox_handlers(
        session_factory=lambda: None,
        memory_index_provider=provider,
    )

    with pytest.raises(OSError, match="qdrant unavailable"):
        handlers[CANON_POST_COMMIT_EVENT](_claim())
    handlers[CANON_POST_COMMIT_EVENT](_claim())
    handlers[CANON_POST_COMMIT_EVENT](_claim())

    assert calls == ["provider", "provider", "handle", "handle"]


def test_empty_memory_index_provider_result_is_not_cached(monkeypatch) -> None:
    calls: list[str] = []
    memory_index = object()

    def provider():
        calls.append("provider")
        return None if calls.count("provider") == 1 else memory_index

    monkeypatch.setattr(
        canon_outbox,
        "handle_canon_post_commit_outbox_event",
        lambda _event, *, memory_index, **_kwargs: calls.append("handle"),
    )
    handlers = canon_outbox.build_canon_outbox_handlers(
        session_factory=lambda: None,
        memory_index_provider=provider,
    )

    with pytest.raises(RuntimeError, match="returned no index"):
        handlers[CANON_POST_COMMIT_EVENT](_claim())
    handlers[CANON_POST_COMMIT_EVENT](_claim())

    assert calls == ["provider", "provider", "handle"]


def test_phase3_handler_resolves_service_lazily_once() -> None:
    calls: list[object] = []

    class Service:
        def resolve_event_canon_commit(self, **identity):
            calls.append(("resolve", identity))
            return "canon-1"

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
    handlers[POST_CANON_PHASE3_EVENT](_phase3_claim())
    handlers[POST_CANON_PHASE3_EVENT](_phase3_claim())

    assert calls[0] == "provider"
    assert calls.count("provider") == 1
    assert [item[0] for item in calls[1:]] == ["resolve", "run", "resolve", "run"]
