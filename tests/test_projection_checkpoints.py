from __future__ import annotations

from collections import defaultdict
from types import MappingProxyType, SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from forwin.http.adapters import api_projection_routes
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
    CANON_PROJECTION_EVENT_SCHEMA_VERSION,
    CANON_PROJECTION_REQUESTED_EVENT,
    KNOWLEDGE_PROJECTION_REFRESH_EVENT,
    build_projection_outbox_handlers,
    normalize_projection_kind,
    projection_components_for_kind,
)
from forwin.models.base import Base
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.projection import ProjectionCheckpoint
from forwin.outbox.worker import OutboxClaim


@pytest.fixture
def projection_sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Project.__table__,
            ArcPlanVersion.__table__,
            ChapterPlan.__table__,
            ChapterDraft.__table__,
            ChapterReview.__table__,
            CandidateDraftRecord.__table__,
            CanonCommitRecord.__table__,
            ProjectionCheckpoint.__table__,
            OutboxEvent.__table__,
        ],
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
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
    commit = CanonCommitRecord(
        id=f"commit-{project_id}-{chapter_number}",
        idempotency_key=f"canon-key-{project_id}-{chapter_number}",
        candidate_id=candidate_id or f"candidate-{project_id}-{chapter_number}",
        project_id=project_id,
        chapter_number=chapter_number,
        status="committed",
    )
    session.add(commit)
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
    candidate = CandidateDraftRecord(
        id=f"candidate-{project_id}-{chapter_number}",
        project_id=project_id,
        chapter_plan_id=chapter.id,
        chapter_number=chapter_number,
        candidate_draft_id=draft.id,
        version=1,
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
        CANON_PROJECTION_REQUESTED_EVENT,
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
        event_type=CANON_PROJECTION_REQUESTED_EVENT,
        aggregate_type="project",
        aggregate_id="project-1",
        payload=MappingProxyType(
            {
                "schema_version": CANON_PROJECTION_EVENT_SCHEMA_VERSION,
                "project_id": "project-1",
            }
        ),
        payload_error="",
        worker_id="worker-1",
        lease_epoch=1,
        attempts=1,
    )
    with pytest.raises(ValueError, match="missing required fields"):
        handlers[CANON_PROJECTION_REQUESTED_EVENT](invalid_canon_claim)
    assert len(refresh_calls) == 1


def test_checkpoint_lock_statement_compiles_for_postgresql() -> None:
    statement = checkpoint_for_update_statement("project-1", "obsidian")
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()

    assert "FROM PROJECTION_CHECKPOINTS" in sql
    assert "FOR UPDATE" in sql
