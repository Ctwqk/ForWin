from __future__ import annotations

from collections import defaultdict
from types import MappingProxyType, SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import select, event
from sqlalchemy.dialects import postgresql

from forwin.http.adapters import api_projection_routes
from forwin.canon.outbox_events import (
    CANON_EVENT_SCHEMA_VERSION,
    CANON_PROJECTION_REQUESTED,
)
from forwin.knowledge_system import checkpoints as checkpoint_module
from forwin.knowledge_system.canon_projection import (
    CanonProjectionService,
    ProjectionRefreshError,
)
from forwin.knowledge_system.checkpoints import (
    PROJECTION_COMPONENTS,
    ProjectionCheckpointStore,
    ProjectionEventIdentity,
    ProjectionTarget,
    checkpoint_for_update_statement,
    sanitize_projection_error,
)
from forwin.knowledge_system.projection_jobs import (
    KNOWLEDGE_PROJECTION_REFRESH_EVENT,
    build_projection_outbox_handlers,
    normalize_projection_kind,
    projection_components_for_kind,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from tests.postgres import postgres_test_url
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.projection import ProjectionCheckpoint
from forwin.outbox.worker import OutboxClaim


@pytest.fixture
def projection_sessions():
    engine = get_engine(postgres_test_url("projection-checkpoints"))
    init_db(engine)
    sessions = get_session_factory(engine)
    try:
        yield sessions
    finally:
        engine.dispose()


def _add_project(session, project_id: str = "project-1") -> Project:
    project = Project(
        id=project_id,
        title="Projection checkpoints",
        premise="Monotonic projection state.",
        genre="thriller",
    )
    session.add(project)
    session.flush()
    return project


def _add_commit(
    session,
    project_id: str,
    chapter_number: int,
    *,
    candidate_id: str | None = None,
) -> CanonCommitRecord:
    candidate = session.get(
        CandidateDraftRecord, candidate_id or f"candidate-{project_id}-{chapter_number}"
    )
    if candidate is None:
        return _add_accepted_chapter(session, project_id, chapter_number)
    chapter = session.get(ChapterPlan, candidate.chapter_plan_id)
    commit = CanonCommitRecord(
        id=f"commit-{project_id}-{chapter_number}",
        idempotency_key=f"canon-key-{project_id}-{chapter_number}",
        candidate_id=candidate_id or f"candidate-{project_id}-{chapter_number}",
        project_id=project_id,
        chapter_plan_id=chapter.id,
        chapter_title=chapter.title,
        chapter_number=chapter_number,
        status="committed",
        base_book_revision=session.get(Project, project_id).book_revision,
    )
    session.add(commit)
    session.flush()
    chapter.active_commit_id = commit.id
    session.get(Project, project_id).book_revision += 1
    session.flush()
    return commit


def _add_accepted_chapter(
    session,
    project_id: str,
    chapter_number: int,
) -> CanonCommitRecord:
    arc_id = f"arc-{project_id}"
    if session.get(ArcPlanVersion, arc_id) is None:
        session.add(
            ArcPlanVersion(
                id=arc_id,
                project_id=project_id,
                arc_synopsis="Arc",
                chapter_start=1,
                chapter_end=10,
            )
        )
        session.flush()
    chapter = ChapterPlan(
        id=f"chapter-{project_id}-{chapter_number}",
        project_id=project_id,
        arc_plan_id=arc_id,
        chapter_number=chapter_number,
        title=f"Chapter {chapter_number}",
        status="accepted",
    )
    session.add(chapter)
    session.flush()
    draft = ChapterDraft(
        id=f"draft-{project_id}-{chapter_number}",
        chapter_plan_id=chapter.id,
        version=1,
        body_text=f"Body {chapter_number}",
        summary=f"Summary {chapter_number}",
        char_count=10,
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(draft_id=draft.id, verdict="pass")
    session.add(review)
    session.flush()
    candidate = CandidateDraftRecord(
        id=f"candidate-{project_id}-{chapter_number}",
        project_id=project_id,
        chapter_plan_id=chapter.id,
        chapter_number=chapter_number,
        candidate_draft_id=draft.id,
        review_id=review.id,
        version=1,
        body_hash=__import__("hashlib").sha256(draft.body_text.encode()).hexdigest(),
        status="accepted",
        canon_status="canon",
        idempotency_key=f"canon-key-{project_id}-{chapter_number}",
    )
    session.add(candidate)
    session.flush()
    commit = _add_commit(
        session,
        project_id,
        chapter_number,
        candidate_id=candidate.id,
    )
    candidate.canon_commit_id = commit.id
    return commit


def _checkpoint(sessions, project_id: str, kind: str) -> ProjectionCheckpoint:
    with sessions() as session:
        return session.execute(
            select(ProjectionCheckpoint).where(
                ProjectionCheckpoint.project_id == project_id,
                ProjectionCheckpoint.projection_kind == kind,
            )
        ).scalar_one()


def test_projection_kind_contract_has_exact_three_checkpoint_owners() -> None:
    assert PROJECTION_COMPONENTS == ("obsidian", "llm_kb", "chapter_memory")
    assert projection_components_for_kind("all") == PROJECTION_COMPONENTS
    assert projection_components_for_kind("world_studio") == (
        "obsidian",
        "llm_kb",
    )
    for kind in PROJECTION_COMPONENTS:
        assert projection_components_for_kind(kind) == (kind,)
    for invalid in ("", "generic", "book_state", "memory", "world"):
        with pytest.raises(ValueError):
            normalize_projection_kind(invalid)


def test_partial_failure_advances_success_and_retry_skips_it(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        _add_commit(session, "project-1", 2)

    calls: defaultdict[str, int] = defaultdict(int)
    llm_should_fail = True

    def runner(kind: str):
        def run(target: ProjectionTarget):
            nonlocal llm_should_fail
            calls[kind] += 1
            assert target.chapter_number == 2
            if kind == "llm_kb" and llm_should_fail:
                return {"ok": False, "error": "api_key=secret unavailable"}
            return {"ok": True, "source_digest": f"digest-{kind}"}

        return run

    service = CanonProjectionService(
        projection_sessions,
        component_runners={kind: runner(kind) for kind in PROJECTION_COMPONENTS},
    )

    with pytest.raises(ProjectionRefreshError) as exc_info:
        service.refresh("project-1", components=PROJECTION_COMPONENTS)

    assert set(exc_info.value.failures) == {"llm_kb"}
    assert "secret" not in exc_info.value.failures["llm_kb"]
    assert _checkpoint(projection_sessions, "project-1", "obsidian").status == "healthy"
    assert _checkpoint(projection_sessions, "project-1", "llm_kb").status == "degraded"
    assert (
        _checkpoint(projection_sessions, "project-1", "chapter_memory").status
        == "healthy"
    )

    llm_should_fail = False
    result = service.refresh("project-1", components=PROJECTION_COMPONENTS)

    assert result["ok"] is True
    assert calls == {"obsidian": 1, "llm_kb": 2, "chapter_memory": 1}
    status = service.checkpoints.status("project-1")
    assert status["healthy"] is True
    assert all(component["lag"] == 0 for component in status["components"])


def test_component_result_object_with_ok_false_is_strict_failure(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        _add_commit(session, "project-1", 1)

    service = CanonProjectionService(
        projection_sessions,
        component_runners={
            "obsidian": lambda _target: SimpleNamespace(
                ok=False,
                error="token=secret rejected",
            )
        },
    )

    with pytest.raises(ProjectionRefreshError) as exc_info:
        service.refresh("project-1", components=("obsidian",))

    assert "secret" not in exc_info.value.failures["obsidian"]
    assert "Bearer" not in sanitize_projection_error(
        "Authorization: Bearer top-secret"
    )
    assert "top-secret" not in sanitize_projection_error(
        "Authorization: Bearer top-secret"
    )
    for structured_error, secret in (
        ('{"Authorization": "Bearer json-secret"}', "json-secret"),
        ("{'api_key': 'sk-live-secret'}", "sk-live-secret"),
        ("headers={'authorization': 'Basic base64-secret'}", "base64-secret"),
    ):
        assert secret not in sanitize_projection_error(structured_error)
    checkpoint = _checkpoint(projection_sessions, "project-1", "obsidian")
    assert checkpoint.status == "degraded"
    assert checkpoint.projected_chapter_number == 0


def test_stale_event_uses_latest_target_and_identity_mismatch_precedes_io(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        old = _add_commit(session, "project-1", 1)
        latest = _add_commit(session, "project-1", 3)

    targets: list[int] = []
    service = CanonProjectionService(
        projection_sessions,
        component_runners={
            "obsidian": lambda target: targets.append(target.chapter_number)
            or {"ok": True}
        },
    )
    identity = ProjectionEventIdentity(
        canon_commit_id=old.id,
        canon_idempotency_key=old.idempotency_key,
        project_id=old.project_id,
        chapter_number=old.chapter_number,
        candidate_id=old.candidate_id,
    )

    result = service.refresh(
        "project-1",
        components=("obsidian",),
        event_identity=identity,
    )

    assert result["target_canon_commit_id"] == latest.id
    assert result["target_chapter_number"] == 3
    assert targets == [3]

    bad_identity = ProjectionEventIdentity(
        canon_commit_id=old.id,
        canon_idempotency_key=old.idempotency_key,
        project_id=old.project_id,
        chapter_number=old.chapter_number,
        candidate_id="wrong-candidate",
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        service.refresh(
            "project-1",
            components=("llm_kb",),
            event_identity=bad_identity,
        )
    assert targets == [3]


def test_target_advance_during_external_io_forces_latest_full_rebuild(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        _add_commit(session, "project-1", 1)

    calls: defaultdict[str, list[int]] = defaultdict(list)
    target_advanced = False
    service: CanonProjectionService

    def run(kind: str):
        def runner(target: ProjectionTarget):
            nonlocal target_advanced
            calls[kind].append(target.chapter_number)
            if kind == "obsidian" and target.chapter_number == 1 and not target_advanced:
                target_advanced = True
                with projection_sessions.begin() as session:
                    _add_commit(session, "project-1", 2)
                latest = service.checkpoints.resolve_target("project-1")
                concurrent_ticket = service.checkpoints.begin_component(
                    latest,
                    "obsidian",
                    event_id="concurrent-newer-worker",
                )
                assert concurrent_ticket is not None
                service.checkpoints.complete_component(
                    concurrent_ticket,
                    source_digest="newer-worker-digest",
                )
            return {"ok": True, "source_digest": f"{kind}-{target.chapter_number}"}

        return runner

    service = CanonProjectionService(
        projection_sessions,
        component_runners={kind: run(kind) for kind in PROJECTION_COMPONENTS},
    )

    result = service.refresh("project-1", components=PROJECTION_COMPONENTS)

    assert result["target_chapter_number"] == 2
    assert calls == {
        "obsidian": [1, 2],
        "llm_kb": [1, 2],
        "chapter_memory": [1, 2],
    }
    status = service.checkpoints.status("project-1")
    assert status["healthy"] is True
    assert all(item["projected_chapter_number"] == 2 for item in status["components"])


def test_stale_runner_failure_after_write_forces_latest_repair(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        _add_commit(session, "project-1", 1)

    calls: list[int] = []
    external_state = {"chapter": 0}
    target_advanced = False
    service: CanonProjectionService

    def runner(target: ProjectionTarget):
        nonlocal target_advanced
        calls.append(target.chapter_number)
        external_state["chapter"] = target.chapter_number
        if target.chapter_number == 1 and not target_advanced:
            target_advanced = True
            with projection_sessions.begin() as session:
                _add_commit(session, "project-1", 2)
            latest = service.checkpoints.resolve_target("project-1")
            concurrent_ticket = service.checkpoints.begin_component(
                latest,
                "obsidian",
                event_id="concurrent-newer-worker",
            )
            assert concurrent_ticket is not None
            external_state["chapter"] = 2
            service.checkpoints.complete_component(
                concurrent_ticket,
                source_digest="newer-worker-digest",
            )
            external_state["chapter"] = 1
            raise RuntimeError("old projection failed after stale write")
        return {"ok": True, "source_digest": f"obsidian-{target.chapter_number}"}

    service = CanonProjectionService(
        projection_sessions,
        component_runners={"obsidian": runner},
    )

    result = service.refresh("project-1", components=("obsidian",))

    assert result["ok"] is True
    assert result["target_chapter_number"] == 2
    assert calls == [1, 2]
    assert external_state["chapter"] == 2
    checkpoint = _checkpoint(projection_sessions, "project-1", "obsidian")
    assert checkpoint.status == "healthy"
    assert checkpoint.projected_chapter_number == 2
    assert checkpoint.source_digest == "obsidian-2"


def test_older_completion_and_failure_cannot_regress_new_success(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        _add_commit(session, "project-1", 1)
    store = ProjectionCheckpointStore(projection_sessions)
    target_one = store.resolve_target("project-1")
    old_ticket = store.begin_component(target_one, "obsidian", event_id="old")
    assert old_ticket is not None

    with projection_sessions.begin() as session:
        _add_commit(session, "project-1", 2)
    target_two = store.resolve_target("project-1")
    new_ticket = store.begin_component(target_two, "obsidian", event_id="new")
    assert new_ticket is not None
    store.complete_component(new_ticket, source_digest="new-digest")

    store.complete_component(old_ticket, source_digest="old-digest")
    store.fail_component(old_ticket, RuntimeError("old failure"))

    row = _checkpoint(projection_sessions, "project-1", "obsidian")
    assert row.status == "healthy"
    assert row.target_chapter_number == 2
    assert row.projected_chapter_number == 2
    assert row.target_canon_commit_id == target_two.canon_commit_id
    assert row.projected_canon_commit_id == target_two.canon_commit_id
    assert row.source_digest == "new-digest"
    assert row.last_event_id == "new"
    assert row.last_error == ""


def test_same_microsecond_runs_still_fence_stale_failure(
    projection_sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        _add_commit(session, "project-1", 1)
    fixed_now = checkpoint_module._utcnow()
    monkeypatch.setattr(checkpoint_module, "_utcnow", lambda: fixed_now)
    store = ProjectionCheckpointStore(projection_sessions)
    target = store.resolve_target("project-1")

    old_ticket = store.begin_component(target, "obsidian", event_id="old")
    new_ticket = store.begin_component(target, "obsidian", event_id="new")

    assert old_ticket is not None
    assert new_ticket is not None
    assert new_ticket.started_at > old_ticket.started_at
    store.complete_component(new_ticket, source_digest="new-digest")
    store.fail_component(old_ticket, RuntimeError("stale failure"))

    row = _checkpoint(projection_sessions, "project-1", "obsidian")
    assert row.status == "healthy"
    assert row.last_event_id == "new"
    assert row.source_digest == "new-digest"
    assert row.last_error == ""


def test_replay_at_target_performs_zero_component_writes(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
    calls: list[int] = []
    service = CanonProjectionService(
        projection_sessions,
        component_runners={
            "obsidian": lambda target: calls.append(target.chapter_number)
            or {"ok": True}
        },
    )

    service.refresh("project-1", components=("obsidian",))
    replay = service.refresh("project-1", components=("obsidian",))

    assert calls == [0]
    assert replay["components"]["obsidian"]["skipped"] is True


def test_chapter_memory_rebuilds_every_accepted_chapter_through_target(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        for chapter_number in (1, 2, 3):
            _add_accepted_chapter(session, "project-1", chapter_number)

    upserts: list[dict[str, object]] = []
    memory_index = SimpleNamespace(
        upsert_chapter=lambda **kwargs: upserts.append(dict(kwargs))
    )
    service = CanonProjectionService(
        projection_sessions,
        memory_index_provider=lambda: memory_index,
    )

    result = service.refresh("project-1", components=("chapter_memory",))
    replay = service.refresh("project-1", components=("chapter_memory",))

    assert result["components"]["chapter_memory"]["chapters"] == [1, 2, 3]
    assert [item["chapter_number"] for item in upserts] == [1, 2, 3]
    assert [item["project_id"] for item in upserts] == ["project-1"] * 3
    assert upserts[0]["title"] == "Chapter 1"
    assert upserts[2]["body"] == "Body 3"
    assert replay["components"]["chapter_memory"]["skipped"] is True
    assert len(upserts) == 3


def test_status_synthesizes_missing_rows_and_uses_priority(
    projection_sessions,
) -> None:
    with projection_sessions.begin() as session:
        _add_project(session)
        target = _add_commit(session, "project-1", 3)
    store = ProjectionCheckpointStore(projection_sessions)

    missing = store.status("project-1")

    assert missing["status"] == "never"
    assert missing["healthy"] is False
    assert [item["projection_kind"] for item in missing["components"]] == list(
        PROJECTION_COMPONENTS
    )
    assert all(item["lag"] == 3 for item in missing["components"])

    with projection_sessions.begin() as session:
        session.add_all(
            [
                ProjectionCheckpoint(
                    project_id="project-1",
                    projection_kind="obsidian",
                    status="healthy",
                    target_canon_commit_id=target.id,
                    target_chapter_number=3,
                    projected_canon_commit_id=target.id,
                    projected_chapter_number=3,
                ),
                ProjectionCheckpoint(
                    project_id="project-1",
                    projection_kind="llm_kb",
                    status="running",
                    target_canon_commit_id=target.id,
                    target_chapter_number=3,
                    projected_chapter_number=2,
                ),
                ProjectionCheckpoint(
                    project_id="project-1",
                    projection_kind="chapter_memory",
                    status="degraded",
                    target_canon_commit_id=target.id,
                    target_chapter_number=3,
                    projected_chapter_number=1,
                    last_error="bounded failure",
                ),
            ]
        )

    status = store.status("project-1")

    assert status["status"] == "degraded"
    assert status["healthy"] is False
    assert [item["lag"] for item in status["components"]] == [0, 1, 2]


def test_immediate_http_failure_is_503_and_deferred_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits: list[str] = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def commit(self) -> None:
            commits.append("commit")

    monkeypatch.setattr(api_projection_routes, "require_project", lambda *_args: None)
    error = ProjectionRefreshError(
        project_id="project-1",
        target=ProjectionTarget(project_id="project-1", chapter_number=2),
        failures={"llm_kb": "RuntimeError: unavailable"},
        results={"obsidian": {"ok": True}, "llm_kb": {"ok": False}},
    )

    def fail_refresh(**_kwargs):
        raise error

    monkeypatch.setattr(api_projection_routes, "refresh_projection_now", fail_refresh)
    handlers = api_projection_routes.build_handlers(get_session=Session)

    with pytest.raises(HTTPException) as exc_info:
        handlers["refresh_projection"]("project-1", projection_kind="all")
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["ok"] is False

    monkeypatch.setattr(
        api_projection_routes,
        "enqueue_projection_refresh",
        lambda *_args, **_kwargs: SimpleNamespace(
            event_id="event-1",
            id="row-1",
        ),
    )
    deferred = handlers["refresh_projection"](
        "project-1",
        projection_kind="all",
        defer=True,
    )
    assert deferred["ok"] is True
    assert deferred["deferred"] is True
    assert deferred["event_type"] == KNOWLEDGE_PROJECTION_REFRESH_EVENT
    assert commits == ["commit"]


def test_handler_registry_is_inert_and_canon_schema_fails_before_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_calls: list[dict[str, object]] = []
    provider_calls: list[str] = []

    def refresh(**kwargs):
        refresh_calls.append(dict(kwargs))
        provider = kwargs["memory_index_provider"]
        provider()
        return {"ok": True}

    from forwin.knowledge_system import projection_jobs

    monkeypatch.setattr(projection_jobs, "refresh_projection_now", refresh)
    handlers = build_projection_outbox_handlers(
        session_factory=object(),
        memory_index_provider=lambda: provider_calls.append("memory") or object(),
    )

    assert set(handlers) == {
        KNOWLEDGE_PROJECTION_REFRESH_EVENT,
        CANON_PROJECTION_REQUESTED,
    }
    assert provider_calls == []

    operator_claim = OutboxClaim(
        row_id="row-1",
        event_id="event-1",
        event_type=KNOWLEDGE_PROJECTION_REFRESH_EVENT,
        aggregate_type="project",
        aggregate_id="project-1",
        payload=MappingProxyType(
            {"project_id": "project-1", "projection_kind": "chapter_memory"}
        ),
        payload_error="",
        worker_id="worker-1",
        lease_epoch=1,
        attempts=1,
    )
    handlers[KNOWLEDGE_PROJECTION_REFRESH_EVENT](operator_claim)
    assert provider_calls == ["memory"]
    assert len(refresh_calls) == 1

    invalid_canon_claim = OutboxClaim(
        row_id="row-2",
        event_id="event-2",
        event_type=CANON_PROJECTION_REQUESTED,
        aggregate_type="project",
        aggregate_id="project-1",
        payload=MappingProxyType(
            {
                "schema_version": CANON_EVENT_SCHEMA_VERSION,
                "project_id": "project-1",
            }
        ),
        payload_error="",
        worker_id="worker-1",
        lease_epoch=1,
        attempts=1,
    )
    with pytest.raises(ValueError, match="canon_commit_id"):
        handlers[CANON_PROJECTION_REQUESTED](invalid_canon_claim)
    assert len(refresh_calls) == 1


def test_checkpoint_lock_statement_compiles_for_postgresql() -> None:
    statement = checkpoint_for_update_statement("project-1", "obsidian")
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()

    assert "FROM PROJECTION_CHECKPOINTS" in sql
    assert "FOR UPDATE" in sql


def test_incremental_memory_only_reads_chapter_100_and_replay_is_empty(projection_sessions):
    with projection_sessions.begin() as session:
        _add_project(session)
        for n in range(1, 100):
            _add_accepted_chapter(session, "project-1", n)
    from forwin.retrieval.memory_index import HashTextEmbedder, QdrantChapterMemoryIndex
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels
    embedded = []
    class CountingEmbedder(HashTextEmbedder):
        def embed(self, texts):
            embedded.extend(texts)
            return super().embed(texts)
    memory_index = QdrantChapterMemoryIndex(url=":memory:", collection_name="incremental", embedder=CountingEmbedder(dims=8), client=FakeQdrantClient(), qdrant_models=FakeQdrantModels)
    service = CanonProjectionService(projection_sessions, memory_index_provider=lambda: memory_index)
    service.refresh("project-1", components=["chapter_memory"])
    assert len(embedded) == 99
    embedded.clear()
    with projection_sessions.begin() as session:
        _add_accepted_chapter(session, "project-1", 100)
    body_reads = []
    def record_body_read(_connection, _cursor, statement, parameters, _context, _many):
        if "chapter_drafts.body_text" in statement:
            body_reads.append((statement, parameters))
    engine = projection_sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_body_read)
    try:
        result = service.refresh("project-1", components=["chapter_memory"])
    finally:
        event.remove(engine, "before_cursor_execute", record_body_read)
    assert len(body_reads) == 1
    assert "canon_commit_records.id IN" in body_reads[0][0]
    assert [value for value in body_reads[0][1].values() if str(value).startswith("commit-")] == ["commit-project-1-100"]
    assert result["components"]["chapter_memory"]["chapters"] == [100]
    assert embedded == ["Chapter 100\nSummary 100\nBody 100"]
    service.refresh("project-1", components=["chapter_memory"])
    assert len(embedded) == 1


def test_chapter_zero_revision_runs_again_and_old_ticket_is_fenced(projection_sessions):
    with projection_sessions.begin() as session:
        _add_project(session)
    calls = []
    service = CanonProjectionService(projection_sessions, component_runners={"obsidian": lambda target: calls.append(target) or {"ok": True}})
    service.refresh("project-1", components=["obsidian"])
    old_target = service.checkpoints.resolve_target("project-1")
    old_ticket = service.checkpoints.begin_component(old_target, "obsidian", force=True)
    with projection_sessions.begin() as session:
        session.get(Project, "project-1").book_revision += 1
    service.refresh("project-1", components=["obsidian"])
    assert len(calls) == 2
    service.checkpoints.complete_component(old_ticket, source_digest="old")
    checkpoint = _checkpoint(projection_sessions, "project-1", "obsidian")
    assert checkpoint.projected_book_revision == 1
    assert checkpoint.source_digest != "old"


def _replace_suffix(session, numbers):
    project = session.get(Project, "project-1")
    commits = []
    for n in numbers:
        chapter = session.get(ChapterPlan, f"chapter-project-1-{n}")
        old = session.get(CanonCommitRecord, chapter.active_commit_id)
        commit = CanonCommitRecord(id=f"{old.id}-rev", idempotency_key=f"{old.idempotency_key}-rev", candidate_id=old.candidate_id,
            project_id=old.project_id, chapter_plan_id=chapter.id, chapter_number=n, chapter_title=old.chapter_title,
            acceptance_revision=old.acceptance_revision + 1, base_book_revision=project.book_revision, status="committed")
        session.add(commit)
        session.flush()
        chapter.active_commit_id = commit.id
        commits.append(commit)
    project.book_revision += 1
    return commits


def test_same_height_suffix_coalesces_all_revisions_and_superseded_event(projection_sessions):
    with projection_sessions.begin() as session:
        _add_project(session)
        for n in (1, 2, 3):
            _add_accepted_chapter(session, "project-1", n)
        old = session.get(CanonCommitRecord, "commit-project-1-3")
        identity = ProjectionEventIdentity(old.id, old.idempotency_key, old.project_id, old.chapter_number, old.candidate_id)
    upserts = []
    service = CanonProjectionService(projection_sessions, memory_index_provider=lambda: SimpleNamespace(upsert_chapter=lambda **kw: upserts.append(kw)))
    service.refresh("project-1", components=["chapter_memory"])
    upserts.clear()
    with projection_sessions.begin() as session:
        _replace_suffix(session, [2, 3])
    with projection_sessions.begin() as session:
        _replace_suffix(session, [3])
    result = service.refresh("project-1", components=["chapter_memory"], event_identity=identity)
    assert result["components"]["chapter_memory"]["chapters"] == [2, 3]
    assert len(upserts) == 2
    assert _checkpoint(projection_sessions, "project-1", "chapter_memory").projected_book_revision == 5
    assert service.refresh("project-1", components=["chapter_memory"], event_identity=identity)["components"]["chapter_memory"]["skipped"]


def test_healthy_chapter_zero_replays_only_after_revision_advance(projection_sessions):
    with projection_sessions.begin() as session:
        _add_project(session)
    calls = []
    service = CanonProjectionService(projection_sessions, component_runners={"obsidian": lambda target: calls.append(target) or {"ok": True}})
    service.refresh("project-1", components=["obsidian"])
    service.refresh("project-1", components=["obsidian"])
    assert len(calls) == 1
    with projection_sessions.begin() as session:
        session.get(Project, "project-1").book_revision += 1
    service.refresh("project-1", components=["obsidian"])
    assert len(calls) == 2


def test_unknown_migrated_checkpoint_rebuilds_once_then_empty_world_revision(projection_sessions):
    with projection_sessions.begin() as session:
        _add_project(session)
        _add_accepted_chapter(session, "project-1", 1)
        session.add(ProjectionCheckpoint(project_id="project-1", projection_kind="chapter_memory", status="healthy", target_chapter_number=1, projected_chapter_number=1))
    upserts = []
    service = CanonProjectionService(projection_sessions, memory_index_provider=lambda: SimpleNamespace(upsert_chapter=lambda **kw: upserts.append(kw)))
    result = service.refresh("project-1", components=["chapter_memory"])
    assert result["components"]["chapter_memory"]["rebuild"] is True
    assert len(upserts) == 1
    service.refresh("project-1", components=["chapter_memory"])
    with projection_sessions.begin() as session:
        session.get(Project, "project-1").book_revision += 1
    result = service.refresh("project-1", components=["chapter_memory"])
    assert result["components"]["chapter_memory"]["chapters"] == []
    assert result["components"]["chapter_memory"]["rebuild"] is False
    assert len(upserts) == 1
    assert _checkpoint(projection_sessions, "project-1", "chapter_memory").projected_book_revision == 2


def test_failed_memory_projection_restart_reuses_cache_then_checkpoints(projection_sessions):
    from forwin.retrieval.memory_index import HashTextEmbedder, QdrantChapterMemoryIndex
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels
    with projection_sessions.begin() as session:
        _add_project(session)
        _add_accepted_chapter(session, "project-1", 1)
    calls = []
    class CountingEmbedder(HashTextEmbedder):
        def embed(self, texts):
            calls.extend(texts)
            return super().embed(texts)
    class FailedClient(FakeQdrantClient):
        def upsert(self, **kwargs):
            raise RuntimeError("point write unavailable")
    def service(client):
        index = QdrantChapterMemoryIndex(url=":memory:", collection_name="restart", embedder=CountingEmbedder(dims=8), client=client, qdrant_models=FakeQdrantModels)
        return CanonProjectionService(projection_sessions, memory_index_provider=lambda: index)
    with pytest.raises(ProjectionRefreshError):
        service(FailedClient()).refresh("project-1", components=["chapter_memory"], event_id="same-event")
    assert _checkpoint(projection_sessions, "project-1", "chapter_memory").status == "degraded"
    assert len(calls) == 1
    restarted = service(FakeQdrantClient())
    restarted.refresh("project-1", components=["chapter_memory"], event_id="same-event")
    replay = restarted.refresh("project-1", components=["chapter_memory"], event_id="same-event")
    assert replay["components"]["chapter_memory"]["skipped"]
    assert len(calls) == 1
    assert _checkpoint(projection_sessions, "project-1", "chapter_memory").projected_book_revision == 1


def test_public_projection_status_preserves_revision_lag_and_unknown_baseline():
    from forwin.api_schema.projection import ProjectionStatusResponse, ProjectionRefreshResponse
    payload = {"project_id": "p", "status": "degraded", "target_book_revision": 5, "components": [
        {"projection_kind": "chapter_memory", "status": "degraded", "target_book_revision": 5, "projected_book_revision": 4, "revision_lag": 1},
        {"projection_kind": "llm_kb", "status": "never", "target_book_revision": 5, "projected_book_revision": None, "revision_lag": None},
    ]}
    result = ProjectionStatusResponse.model_validate(payload).model_dump()
    assert result.get("target_book_revision") == 5
    assert result["components"][0].get("projected_book_revision") == 4
    assert result["components"][0].get("revision_lag") == 1
    assert "projected_book_revision" in result["components"][1]
    assert result["components"][1]["projected_book_revision"] is None
    assert result["components"][1]["revision_lag"] is None
    assert ProjectionRefreshResponse.model_validate({"project_id": "p", "target_book_revision": 5}).model_dump().get("target_book_revision") == 5


def test_http_projection_callable_factory_persists_cold_cache_and_replays(projection_sessions):
    from forwin.models.embedding import EmbeddingCacheEntry
    from forwin.retrieval.memory_index import HashTextEmbedder, QdrantChapterMemoryIndex
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels

    with projection_sessions.begin() as session:
        _add_project(session)
        _add_accepted_chapter(session, "project-1", 1)
    embedded = []

    class CountingEmbedder(HashTextEmbedder):
        def embed(self, texts):
            embedded.extend(texts)
            return super().embed(texts)

    client = FakeQdrantClient()
    index = QdrantChapterMemoryIndex(
        url=":memory:", collection_name="http-cache",
        embedder=CountingEmbedder(dims=8), client=client,
        qdrant_models=FakeQdrantModels,
    )
    handlers = api_projection_routes.build_handlers(
        get_session=lambda: projection_sessions(),
        memory_index_provider=lambda: index,
    )
    first = handlers["refresh_projection"]("project-1", projection_kind="chapter_memory")
    assert first["ok"] is True
    assert first["components"]["chapter_memory"]["chapters"] == [1]
    with projection_sessions() as session:
        cache = session.scalars(select(EmbeddingCacheEntry)).all()
        assert len(cache) == 1
    assert len(client.collections[index.collection_name]["points"]) == 1
    replay = handlers["refresh_projection"]("project-1", projection_kind="chapter_memory")
    assert replay["components"]["chapter_memory"]["skipped"] is True
    assert embedded == ["Chapter 1\nSummary 1\nBody 1"]
    checkpoint = _checkpoint(projection_sessions, "project-1", "chapter_memory")
    assert checkpoint.status == "healthy"
    assert checkpoint.projected_book_revision == 1
