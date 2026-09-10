from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from forwin.canon.admission import CanonAdmissionService, CanonStaleVersion
from forwin.generation.pipeline_core.world_projection import PostCanonStage
from forwin.http.adapters.api_maintenance_routes import _chapter_info
from forwin.maintenance.events import ORDER_CONTROLS_KEY, POST_CANON_STEP_NAMES
from forwin.maintenance.post_canon import (
    PostCanonLeaseLost,
    PostCanonMaintenanceError,
    PostCanonMaintenanceService,
    PostCanonRunClaim,
    _ClaimHeartbeat,
    post_canon_idempotency_key,
)
from forwin.maintenance.state import (
    post_canon_barrier_ready,
    post_canon_checkpoint_status,
)
from forwin.models.base import Base
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.planning_control import BandCheckpoint
from forwin.models.project import ChapterPlan, Project
from forwin.runtime.policy import RuntimePolicy
from forwin.storage.artifacts import ArtifactStore


def _run(
    step_name: str,
    *,
    status: str = "succeeded",
    result: dict[str, object] | None = None,
    lease_expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"run-{step_name}",
        canon_commit_id="canon-1",
        step_name=step_name,
        status=status,
        attempts=1,
        worker_id="worker-1" if status == "running" else "",
        lease_epoch=1,
        lease_expires_at=lease_expires_at,
        heartbeat_at=None,
        last_error="",
        result_json=json.dumps(result or {}, sort_keys=True),
        started_at=None,
        completed_at=None,
        updated_at=None,
    )


def _completed_rows(*, checkpoint_status: str = "pass") -> list[SimpleNamespace]:
    rows = [_run(step_name) for step_name in POST_CANON_STEP_NAMES]
    rows[-1].result_json = json.dumps(
        {
            ORDER_CONTROLS_KEY: {
                "status": "succeeded",
                "result": {
                    "blocking_reasons": [],
                    "checkpoint": {
                        "id": "checkpoint-1",
                        "status": checkpoint_status,
                    },
                },
            }
        },
        sort_keys=True,
    )
    return rows


def _durable_service():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            Project(
                id="project-1",
                title="Project",
                premise="Premise",
                runtime_policy_json=(
                    RuntimePolicy.for_profile("standard").model_dump_json()
                ),
                runtime_policy_version=1,
            )
        )
        session.add(
            CandidateDraftRecord(
                id="candidate-1",
                project_id="project-1",
                chapter_plan_id="plan-1",
                chapter_number=1,
                candidate_draft_id="draft-1",
                canon_commit_id="canon-1",
                idempotency_key="candidate-key-1",
                status="accepted",
            )
        )
        session.add(
            CanonCommitRecord(
                id="canon-1",
                chapter_plan_id="plan-1",
                idempotency_key="canon-key-1",
                candidate_id="candidate-1",
                project_id="project-1",
                chapter_number=1,
                status="committed",
            )
        )
        session.add(ChapterPlan(id="plan-1", project_id="project-1", arc_plan_id="arc-1",
                                chapter_number=1, status="accepted", active_commit_id="canon-1"))
    calls: list[str] = []
    service = PostCanonMaintenanceService(
        session_factory=sessions,
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=SimpleNamespace(drain_llm_attempt_events=lambda: []),
    )
    service.heartbeat = lambda _claim: None  # type: ignore[method-assign]
    service._step_runners = {
        step_name: (
            lambda _session, _commit, name=step_name: (
                calls.append(name) or {"step": name}
            )
        )
        for step_name in POST_CANON_STEP_NAMES
    }
    return service, sessions, calls


def test_durable_runs_materialize_once_execute_in_order_and_skip_on_replay() -> None:
    service, sessions, calls = _durable_service()

    first = service.run(canon_commit_id="canon-1", worker_id="worker-1")
    second = service.run(canon_commit_id="canon-1", worker_id="worker-2")

    with sessions() as session:
        rows = list(
            session.execute(
                select(PostCanonMaintenanceRun).where(
                    PostCanonMaintenanceRun.canon_commit_id == "canon-1"
                )
            ).scalars()
        )
    by_step = {row.step_name: row for row in rows}
    assert calls == list(POST_CANON_STEP_NAMES)
    assert len(rows) == len(POST_CANON_STEP_NAMES)
    assert set(by_step) == set(POST_CANON_STEP_NAMES)
    assert all(row.status == "succeeded" for row in rows)
    assert all(row.attempts == 1 for row in rows)
    assert [
        by_step[name].idempotency_key for name in POST_CANON_STEP_NAMES
    ] == [
        f"post-canon-maintenance:v1:canon-key-1:{name}"
        for name in POST_CANON_STEP_NAMES
    ]
    assert first["run_ids"] == second["run_ids"]


def test_expired_run_is_reclaimed_and_old_epoch_cannot_complete() -> None:
    service, sessions, _calls = _durable_service()
    service._ensure_runs("canon-1")
    with sessions.begin() as session:
        row = session.execute(
            select(PostCanonMaintenanceRun).where(
                PostCanonMaintenanceRun.canon_commit_id == "canon-1",
                PostCanonMaintenanceRun.step_name == "planning",
            )
        ).scalar_one()
        row.status = "running"
        row.attempts = 1
        row.worker_id = "old-worker"
        row.lease_epoch = 1
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    claim, _run_id = service._claim_step(
        canon_commit_id="canon-1",
        step_name="planning",
        worker_id="new-worker",
    )

    assert claim is not None
    assert claim.worker_id == "new-worker"
    assert claim.lease_epoch == 2
    stale_claim = PostCanonRunClaim(
        run_id=claim.run_id,
        canon_commit_id=claim.canon_commit_id,
        project_id=claim.project_id,
        chapter_number=claim.chapter_number,
        candidate_id=claim.candidate_id,
        step_name=claim.step_name,
        worker_id="old-worker",
        lease_epoch=1,
        lease_seconds=claim.lease_seconds,
    )
    with pytest.raises(PostCanonLeaseLost):
        with sessions.begin() as session:
            service._complete_claim_in_session(
                session,
                stale_claim,
                {"stale": True},
            )


def test_heartbeat_cleanup_is_safe_before_thread_start() -> None:
    heartbeat = _ClaimHeartbeat(
        service=SimpleNamespace(),
        claim=SimpleNamespace(run_id="run-1"),
        interval_seconds=0.01,
    )

    heartbeat.stop_and_join()

    assert heartbeat.ownership_lost is False


def test_failure_write_is_fenced_by_unexpired_lease() -> None:
    statements = []

    class FakeSession:
        def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

    service = SimpleNamespace(
        session_factory=SimpleNamespace(begin=lambda: nullcontext(FakeSession())),
        _clock=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    claim = PostCanonRunClaim(
        run_id="run-1",
        canon_commit_id="canon-1",
        project_id="project-1",
        chapter_number=1,
        candidate_id="candidate-1",
        step_name="planning",
        worker_id="worker-1",
        lease_epoch=2,
        lease_seconds=300,
    )

    PostCanonMaintenanceService._fail_claim(service, claim, RuntimeError("failed"))

    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "lease_expires_at IS NOT NULL" in sql
    assert "lease_expires_at >" in sql


def test_trace_enqueue_failure_rolls_back_step_and_marks_run_retryable() -> None:
    transaction_errors: list[type[BaseException] | None] = []
    failed_claims = []
    commit = SimpleNamespace(id="canon-1", status="committed", chapter_plan_id="plan-1", active_commit_id="canon-1", project_id="project-1", chapter_number=1)

    class FakeSession:
        def get(self, _model, _identity):
            return commit

        def execute(self, _statement):
            return SimpleNamespace(rowcount=1)

    class Transaction:
        def __enter__(self):
            return FakeSession()

        def __exit__(self, exc_type, _exc, _traceback):
            transaction_errors.append(exc_type)
            return False

    service = PostCanonMaintenanceService(
        session_factory=SimpleNamespace(begin=Transaction),
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=SimpleNamespace(drain_llm_attempt_events=lambda: []),
    )
    service.heartbeat = lambda _claim: None  # type: ignore[method-assign]
    service._step_runners["world"] = lambda _session, _commit: {"ok": True}
    service._enqueue_step_trace = (  # type: ignore[method-assign]
        lambda **_kwargs: (_ for _ in ()).throw(OSError("outbox unavailable"))
    )
    service._fail_claim = (  # type: ignore[method-assign]
        lambda claim, _exc: failed_claims.append(claim)
    )
    claim = PostCanonRunClaim(
        run_id="run-world",
        canon_commit_id="canon-1",
        project_id="project-1",
        chapter_number=1,
        candidate_id="candidate-1",
        step_name="world",
        worker_id="worker-1",
        lease_epoch=1,
        lease_seconds=300,
    )

    with pytest.raises(PostCanonMaintenanceError, match="outbox unavailable"):
        service._execute_claim(claim)

    assert transaction_errors == [OSError]
    assert failed_claims == [claim]


def test_stale_completion_fence_is_rejected() -> None:
    service = SimpleNamespace(
        _clock=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    session = SimpleNamespace(
        execute=lambda _statement: SimpleNamespace(rowcount=0),
    )
    claim = PostCanonRunClaim(
        run_id="run-1",
        canon_commit_id="canon-1",
        project_id="project-1",
        chapter_number=1,
        candidate_id="candidate-1",
        step_name="planning",
        worker_id="stale-worker",
        lease_epoch=1,
        lease_seconds=300,
    )

    with pytest.raises(PostCanonLeaseLost, match="stale post-Canon worker"):
        PostCanonMaintenanceService._acquire_completion_fence(
            service,
            session,
            claim,
        )


def test_stale_completion_cannot_enqueue_trace() -> None:
    commit = SimpleNamespace(id="canon-1", status="committed", chapter_plan_id="plan-1", active_commit_id="canon-1", project_id="project-1", chapter_number=1)

    class FakeSession:
        def get(self, _model, _identity):
            return commit

        def execute(self, _statement):
            return SimpleNamespace(rowcount=0)

    drains = iter(([], [{"attempt_no": 1}], []))
    service = PostCanonMaintenanceService(
        session_factory=SimpleNamespace(
            begin=lambda: nullcontext(FakeSession()),
        ),
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=SimpleNamespace(
            drain_llm_attempt_events=lambda: next(drains),
        ),
    )
    service.heartbeat = lambda _claim: None  # type: ignore[method-assign]
    service._step_runners["world"] = lambda _session, _commit: {"ok": True}
    trace_writes: list[str] = []
    service._enqueue_step_trace = (  # type: ignore[method-assign]
        lambda **kwargs: trace_writes.append(str(kwargs["step_name"])) or {}
    )
    claim = PostCanonRunClaim(
        run_id="run-world",
        canon_commit_id="canon-1",
        project_id="project-1",
        chapter_number=1,
        candidate_id="candidate-1",
        step_name="world",
        worker_id="stale-worker",
        lease_epoch=1,
        lease_seconds=300,
    )

    with pytest.raises(PostCanonLeaseLost, match="stale post-Canon worker"):
        service._execute_claim(claim)

    assert trace_writes == []


def test_barrier_requires_exactly_the_four_canonical_steps() -> None:
    rows = _completed_rows()
    rows.append(_run("obsolete-step"))

    assert post_canon_barrier_ready(rows) is False


def test_order_controls_reject_noncanonical_step_set() -> None:
    rows = _completed_rows()
    rows.append(_run("obsolete-step"))

    with pytest.raises(PostCanonMaintenanceError, match="exactly the canonical steps"):
        PostCanonMaintenanceService._require_all_steps_succeeded(rows, "canon-1")


def test_run_materialization_rejects_unknown_step_rows() -> None:
    service, sessions, _calls = _durable_service()
    with sessions.begin() as session:
        session.add(PostCanonMaintenanceRun(
            canon_commit_id="canon-1", project_id="project-1", chapter_number=1,
            candidate_id="candidate-1", step_name="obsolete-step", idempotency_key="obsolete",
        ))
    with pytest.raises(ValueError, match="unknown post-Canon steps"):
        service._ensure_runs("canon-1")
    with sessions() as session:
        assert len(list(session.scalars(select(PostCanonMaintenanceRun)))) == 1


def test_phase3_event_requires_complete_canon_identity() -> None:
    commit = SimpleNamespace(
        id="canon-1",
        chapter_plan_id="plan-1", active_commit_id="canon-1",
        idempotency_key="canon-key-1",
        project_id="project-1",
        chapter_number=1,
        candidate_id="candidate-1",
        status="committed",
    )
    session = SimpleNamespace(get=lambda _model, _identity: commit)
    service = SimpleNamespace(session_factory=lambda: nullcontext(session))

    with pytest.raises(ValueError, match="candidate_id"):
        PostCanonMaintenanceService.resolve_event_canon_commit(
            service,
            canon_commit_id="canon-1",
            canon_idempotency_key="canon-key-1",
            project_id="project-1",
            chapter_number=1,
            candidate_id="",
        )

    with pytest.raises(ValueError, match="does not match committed Canon"):
        PostCanonMaintenanceService.resolve_event_canon_commit(
            service,
            canon_commit_id="canon-1",
            canon_idempotency_key="wrong-key",
            project_id="project-1",
            chapter_number=1,
            candidate_id="candidate-1",
        )


def test_synchronous_chapter_entrypoint_rejects_mismatched_canon() -> None:
    commit = SimpleNamespace(
        id="canon-1",
        chapter_plan_id="plan-1", active_commit_id="canon-1",
        project_id="other-project",
        chapter_number=1,
        status="committed",
    )
    session = SimpleNamespace(get=lambda _model, _identity: commit)
    service = SimpleNamespace(
        session_factory=lambda: nullcontext(session),
        run=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("mismatched Canon must not run")
        ),
    )

    with pytest.raises(ValueError, match="does not match project/chapter"):
        PostCanonMaintenanceService.run_for_chapter(
            service,
            canon_commit_id="canon-1",
            project_id="project-1",
            chapter_number=1,
            worker_id="worker-1",
        )


def test_pipeline_uses_canonical_top_level_control_blockers() -> None:
    assert PostCanonStage._post_canon_control_blocking_reasons(
        {"blocking_reasons": ["future plan is inconsistent"]}
    ) == ["future plan is inconsistent"]


def test_natural_key_uses_canon_idempotency_identity() -> None:
    assert post_canon_idempotency_key("canon-key-1", "planning") == (
        "post-canon-maintenance:v1:canon-key-1:planning"
    )


def test_keyed_artifact_replay_overwrites_the_same_object(tmp_path: Path) -> None:
    store = ArtifactStore(root_dir=str(tmp_path), backend="local")

    first = store.save_keyed_artifact(
        project_id="project-1",
        artifact_key="post_canon/canon-1/world/llm_trace.json",
        content='{"attempt":1}',
        content_type="application/json",
    )
    second = store.save_keyed_artifact(
        project_id="project-1",
        artifact_key="post_canon/canon-1/world/llm_trace.json",
        content='{"attempt":2}',
        content_type="application/json",
    )

    assert first["artifact_uri"] == second["artifact_uri"]
    assert Path(str(second["artifact_uri"])).read_text(encoding="utf-8") == (
        '{"attempt":2}'
    )


def test_failed_checkpoint_is_not_reported_ready() -> None:
    chapter = _chapter_info(
        SimpleNamespace(id="canon-1", candidate_id="candidate-1", chapter_number=1),
        _completed_rows(checkpoint_status="fail"),
    )

    assert chapter.phase3_complete is True
    assert chapter.controls_complete is True
    assert chapter.ready is False
    assert chapter.status == "blocked"
    assert chapter.blocking_reasons == ["band_checkpoint_fail"]


def test_warn_checkpoint_uses_pause_on_warn_by_default() -> None:
    chapter = _chapter_info(
        SimpleNamespace(id="canon-1", candidate_id="candidate-1", chapter_number=1),
        _completed_rows(checkpoint_status="warn"),
    )

    assert chapter.ready is False
    assert chapter.blocking_reasons == ["band_checkpoint_warn"]


def test_warn_checkpoint_does_not_block_continue_policy() -> None:
    chapter = _chapter_info(
        SimpleNamespace(id="canon-1", candidate_id="candidate-1", chapter_number=1),
        _completed_rows(checkpoint_status="warn"),
        band_checkpoint_action="continue",
    )

    assert chapter.ready is True
    assert chapter.blocking_reasons == []


def test_live_checkpoint_status_overrides_stale_snapshot() -> None:
    chapter = _chapter_info(
        SimpleNamespace(id="canon-1", candidate_id="candidate-1", chapter_number=1),
        _completed_rows(checkpoint_status="fail"),
        checkpoint_status="overridden",
    )

    assert chapter.ready is True
    assert chapter.checkpoint_status == "overridden"


def test_blocked_order_controls_are_re_evaluated_until_clear() -> None:
    service, sessions, _calls = _durable_service()
    service.run(canon_commit_id="canon-1", worker_id="worker-1")
    calls = 0

    results = iter(
        (
            {"blocking_reasons": ["future plan is inconsistent"]},
            {"blocking_reasons": []},
        )
    )

    def runner(_session, _commit):
        nonlocal calls
        calls += 1
        return next(results)

    first = service.run_order_controls(canon_commit_id="canon-1", runner=runner)
    second = service.run_order_controls(canon_commit_id="canon-1", runner=runner)

    assert first["blocking_reasons"] == ["future plan is inconsistent"]
    assert second["blocking_reasons"] == []
    assert calls == 2
    with sessions() as session:
        rows = list(session.scalars(select(PostCanonMaintenanceRun)))
        feedback = next(row for row in rows if row.step_name == "feedback")
    controls = json.loads(feedback.result_json)[ORDER_CONTROLS_KEY]
    assert controls["status"] == "succeeded"
    assert controls["continuation_blocked"] is False
    assert post_canon_barrier_ready(rows) is True


def test_pending_manual_post_acceptance_checkpoint_blocks_durable_barrier() -> None:
    service, sessions, _calls = _durable_service()
    service.run(canon_commit_id="canon-1", worker_id="worker-1")
    service.run_order_controls(
        canon_commit_id="canon-1",
        runner=lambda _session, _commit: {"blocking_reasons": []},
    )
    with sessions.begin() as session:
        session.add(
            BandCheckpoint(
                id="manual-checkpoint-1",
                project_id="project-1",
                arc_id="arc-1",
                trigger_source="manual_boundary",
                boundary_kind="chapter_accepted",
                boundary_chapter=1,
                status="pending",
            )
        )

    assert service.barrier_blocking_reasons("canon-1") == [
        "manual_checkpoint_chapter_accepted_pending"
    ]


def test_continuation_preflight_resumes_checkpoint_delegation() -> None:
    checkpoint = SimpleNamespace(id="checkpoint-1", status="warn")
    project = SimpleNamespace(id="project-1")
    updater = object()
    resolutions: list[dict[str, object]] = []
    commits: list[str] = []
    session = SimpleNamespace(
        get=lambda _model, identity: checkpoint if identity == "checkpoint-1" else None,
        commit=lambda: commits.append("commit"),
    )
    stage = SimpleNamespace(
        _make_state_helpers=lambda _session: (
            SimpleNamespace(get_project=lambda _project_id: project),
            updater,
            object(),
        ),
        _project_policy=lambda _session, _project: RuntimePolicy.for_profile(
            "standard"
        ),
        _resolve_checkpoint_gate=lambda **request: (
            resolutions.append(request) or True
        ),
    )

    PostCanonStage._resume_post_canon_checkpoint_delegation(
        stage,
        session=session,
        project_id="project-1",
        chapter_number=1,
        controls={
            "checkpoint": {
                "id": "checkpoint-1",
                "status": "warn",
            }
        },
    )

    assert resolutions == [
        {
            "updater": updater,
            "checkpoint": checkpoint,
            "gate_kind": "band_checkpoint_pause",
            "chapter_number": 1,
        }
    ]
    assert commits == ["commit"]


def test_canon_admission_rejects_incomplete_previous_barrier() -> None:
    previous = SimpleNamespace(id="canon-1")
    rows = _completed_rows()
    rows[-1].result_json = "{}"

    class Result:
        def __init__(self, values):
            self.values = values

        def scalar_one_or_none(self):
            return self.values

        def scalars(self):
            return iter(self.values)

    class FakeSession:
        def __init__(self):
            self.results = iter((Result(previous), Result(rows)))

        def execute(self, _statement):
            return next(self.results)

    with pytest.raises(CanonStaleVersion, match="incomplete post-Canon"):
        CanonAdmissionService._require_previous_post_canon_barrier(
            session=FakeSession(),
            plan=SimpleNamespace(project_id="project-1", chapter_number=2),
        )


def test_canon_admission_rejects_failed_previous_checkpoint() -> None:
    service, sessions, _calls = _durable_service()
    service.run(canon_commit_id="canon-1", worker_id="worker-1")
    with sessions.begin() as session:
        session.add(BandCheckpoint(
            id="checkpoint-1", project_id="project-1", arc_id="arc-1", status="fail",
            trigger_source="manual_boundary",
            boundary_chapter=1, chapter_start=1, chapter_end=1,
        ))
        feedback = session.scalar(select(PostCanonMaintenanceRun).where(PostCanonMaintenanceRun.step_name == "feedback"))
        feedback.result_json = json.dumps({ORDER_CONTROLS_KEY: {
            "status": "succeeded", "result": {"checkpoint": {"id": "checkpoint-1", "status": "fail"}},
        }})
    with sessions() as session, pytest.raises(CanonStaleVersion, match="incomplete post-Canon"):
        rows = list(session.scalars(select(PostCanonMaintenanceRun)))
        assert post_canon_checkpoint_status(rows, session=session) == "fail"
        CanonAdmissionService._require_previous_post_canon_barrier(
            session=session,
            plan=SimpleNamespace(project_id="project-1", chapter_number=2),
        )


def test_expired_running_lease_is_reported_reclaimable() -> None:
    rows = [
        _run(
            "planning",
            status="running",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
        *[_run(step_name, status="pending") for step_name in POST_CANON_STEP_NAMES[1:]],
    ]

    chapter = _chapter_info(
        SimpleNamespace(id="canon-1", candidate_id="candidate-1", chapter_number=1),
        rows,
    )

    assert chapter.status == "stale"
    assert chapter.runs[0].lease_active is False
    assert chapter.runs[0].reclaimable is True
