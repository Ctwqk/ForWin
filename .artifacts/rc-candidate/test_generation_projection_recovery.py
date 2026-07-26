from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


MODULE_PATH = Path(__file__).with_name("generation_projection_recovery.py")
SPEC = importlib.util.spec_from_file_location(
    "generation_projection_recovery", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

EVALUATOR_PATH = Path(__file__).with_name("recovery_evidence.py")
EVALUATOR_SPEC = importlib.util.spec_from_file_location(
    "task4_test_recovery_evidence", EVALUATOR_PATH
)
assert EVALUATOR_SPEC is not None and EVALUATOR_SPEC.loader is not None
evidence = importlib.util.module_from_spec(EVALUATOR_SPEC)
EVALUATOR_SPEC.loader.exec_module(evidence)

FINALIZER_PATH = Path(__file__).with_name("finalize_recovery.py")
FINALIZER_SPEC = importlib.util.spec_from_file_location(
    "task4_test_finalize_recovery", FINALIZER_PATH
)
assert FINALIZER_SPEC is not None and FINALIZER_SPEC.loader is not None
finalizer = importlib.util.module_from_spec(FINALIZER_SPEC)
FINALIZER_SPEC.loader.exec_module(finalizer)


SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
FAULT_ID = "task4-fault-a"
PROJECT_ID = "project-a"
CHAPTER_ID = "chapter-a"
TASK_ID = "task-a"
CANON_ID = "canon-a"
CANDIDATE_ID = "candidate-a"
BODY_SHA = hashlib.sha256(b"fixture chapter").hexdigest()


def composed_text(statement: Any) -> str:
    as_string = getattr(statement, "as_string", None)
    return as_string() if callable(as_string) else str(statement)


class FakeCursor:
    def __init__(
        self,
        connection: "FakeConnection",
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.connection = connection
        self.rows = list(rows or [])
        self.description = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self, statement: Any, params: tuple[Any, ...] | list[Any] | None = None
    ) -> "FakeCursor":
        text = composed_text(statement)
        values = tuple(params or ())
        self.connection.executions.append((text, values))
        if self.connection.fail_when and self.connection.fail_when(text):
            raise RuntimeError("injected SQL failure")
        if "pg_locks" in text:
            self.rows = copy.deepcopy(self.connection.lock_rows)
        elif "barrier residue" in text:
            self.rows = [{"residue_count": self.connection.residue_count}]
        elif "pg_advisory_unlock" in text:
            self.rows = [{"unlocked": self.connection.unlock_result}]
        else:
            self.rows = []
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.rows[0]) if self.rows else None


class FakeConnection:
    def __init__(
        self,
        *,
        lock_rows: list[dict[str, Any]] | None = None,
        fail_when: Callable[[str], bool] | None = None,
    ) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.lock_rows = list(lock_rows or [])
        self.fail_when = fail_when
        self.residue_count = 0
        self.unlock_result = True
        self.closed = False
        self.autocommit = False

    def cursor(self, **_kwargs: Any) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class FakeConnectionFactory:
    def __init__(self, *connections: FakeConnection) -> None:
        self.connections = list(connections)
        self.urls: list[str] = []

    def __call__(self, database_url: str) -> FakeConnection:
        self.urls.append(database_url)
        if not self.connections:
            raise AssertionError("unexpected database connection")
        return self.connections.pop(0)


def lock_rows(barrier: Any) -> list[dict[str, Any]]:
    return [
        {
            "pid": 401,
            "granted": True,
            "application_name": barrier.holder_application_name,
            "query": "SELECT pg_advisory_lock($1)",
            "wait_event_type": None,
            "wait_event": None,
            "blocking_pids": [],
        },
        {
            "pid": 509,
            "granted": False,
            "application_name": "",
            "query": f"INSERT INTO {barrier.target_table} (...)",
            "wait_event_type": "Lock",
            "wait_event": "advisory",
            "blocking_pids": [401],
        },
    ]


@pytest.mark.parametrize(
    ("kind", "target_table", "step_clause"),
    [
        ("generation_worker_precommit_crash", "canon_commit_records", ""),
        (
            "generation_worker_postcommit_crash",
            "post_canon_maintenance_runs",
            "NEW.step_name = 'planning'",
        ),
    ],
)
def test_barrier_uses_fault_scoped_identifiers_and_bound_business_values(
    kind: str,
    target_table: str,
    step_clause: str,
) -> None:
    admin = FakeConnection()
    holder = FakeConnection()
    barrier = runner.AdvisoryBarrier(
        kind=kind,
        fault_id=FAULT_ID,
        database_url="postgresql://fixture",
        connect=FakeConnectionFactory(admin, holder),
    )

    barrier.install(project_id=PROJECT_ID, chapter_number=1)

    assert barrier.target_table == target_table
    names = {
        barrier.names.scope_table,
        barrier.names.function,
        barrier.names.trigger,
    }
    assert len(names) == 3
    assert all(
        re.fullmatch(r"fw_recovery_[0-9a-f]{16}_(scope|fn|trg)", name)
        for name in names
    )
    assert runner.barrier_names("another-fault") != barrier.names

    statements = "\n".join(text for text, _ in admin.executions)
    assert PROJECT_ID not in statements
    assert FAULT_ID not in statements
    assert target_table in statements
    assert "pg_advisory_xact_lock" in statements
    assert (step_clause in statements) if step_clause else ("NEW.step_name" not in statements)

    scope_insert = next(
        (text, params)
        for text, params in admin.executions
        if text.lstrip().startswith("INSERT INTO")
    )
    assert scope_insert[1] == (PROJECT_ID, 1, barrier.advisory_key)
    assert any(
        params == (barrier.holder_application_name,)
        for _, params in holder.executions
    )
    assert any(
        params == (barrier.advisory_key,)
        for _, params in holder.executions
    )


def test_barrier_requires_exactly_one_holder_and_one_blocked_waiter() -> None:
    admin = FakeConnection()
    holder = FakeConnection()
    factory = FakeConnectionFactory(admin, holder)
    barrier = runner.AdvisoryBarrier(
        kind="generation_worker_precommit_crash",
        fault_id=FAULT_ID,
        database_url="postgresql://fixture",
        connect=factory,
    )
    barrier.install(project_id=PROJECT_ID, chapter_number=1)
    admin.lock_rows = lock_rows(barrier)

    observation = barrier.observe_blocked_waiter()

    assert observation.holder_pid == 401
    assert observation.waiter_pid == 509
    lock_query, lock_params = next(
        (text, params)
        for text, params in admin.executions
        if "pg_locks" in text
    )
    assert "pg_blocking_pids" in lock_query
    assert lock_params == barrier.lock_identity

    admin.lock_rows.append(
        {
            **admin.lock_rows[-1],
            "pid": 510,
        }
    )
    with pytest.raises(runner.SetupBlocked, match="exactly one holder and one waiter"):
        barrier.observe_blocked_waiter()


def test_barrier_cleanup_is_symmetric_after_partial_install_failure() -> None:
    admin = FakeConnection(
        fail_when=lambda statement: statement.lstrip().startswith("CREATE TRIGGER")
    )
    holder = FakeConnection()
    barrier = runner.AdvisoryBarrier(
        kind="generation_worker_postcommit_crash",
        fault_id=FAULT_ID,
        database_url="postgresql://fixture",
        connect=FakeConnectionFactory(admin, holder),
    )

    with pytest.raises(RuntimeError, match="injected SQL failure"):
        barrier.install(project_id=PROJECT_ID, chapter_number=1)

    statements = "\n".join(text for text, _ in admin.executions)
    assert "DROP TRIGGER IF EXISTS" in statements
    assert "DROP FUNCTION IF EXISTS" in statements
    assert "DROP TABLE IF EXISTS" in statements
    assert any("pg_advisory_unlock" in text for text, _ in holder.executions)
    assert holder.closed is True
    assert admin.closed is True


def test_barrier_cleanup_fails_when_residue_remains() -> None:
    admin = FakeConnection()
    holder = FakeConnection()
    barrier = runner.AdvisoryBarrier(
        kind="generation_worker_precommit_crash",
        fault_id=FAULT_ID,
        database_url="postgresql://fixture",
        connect=FakeConnectionFactory(admin, holder),
    )
    barrier.install(project_id=PROJECT_ID, chapter_number=1)
    admin.residue_count = 1

    with pytest.raises(runner.RunnerError, match="barrier residue"):
        barrier.cleanup()


def test_barrier_closes_admin_when_holder_connection_fails() -> None:
    admin = FakeConnection()
    calls = 0

    def connect(_database_url: str) -> FakeConnection:
        nonlocal calls
        calls += 1
        if calls == 1:
            return admin
        raise RuntimeError("holder connect failed")

    barrier = runner.AdvisoryBarrier(
        kind="generation_worker_precommit_crash",
        fault_id=FAULT_ID,
        database_url="postgresql://fixture",
        connect=connect,
    )

    with pytest.raises(RuntimeError, match="holder connect failed"):
        barrier.install(project_id=PROJECT_ID, chapter_number=1)

    assert admin.closed is True


class FakeMCP:
    def __init__(self, *, active: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.active = active

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "project_create":
            return {
                "ok": True,
                "project": {
                    "id": PROJECT_ID,
                    "creation_status": "creating",
                },
            }
        if name == "project_get":
            return {
                "id": PROJECT_ID,
                "creation_status": "genesis_ready",
                "can_start_writing": True,
            }
        if name == "genesis_get":
            return {
                "project_id": PROJECT_ID,
                "can_start_writing": True,
                "stage_states": [],
            }
        if name == "task_active_generation_check":
            return {
                "has_active_generation_task": self.active,
                "active_count": int(self.active),
            }
        if name == "project_start_writing":
            return {
                "ok": True,
                "task": {
                    "task_id": TASK_ID,
                    "project_id": PROJECT_ID,
                    "status": "queued",
                },
            }
        return {"ok": True}


def test_mcp_fixture_reads_before_writes_and_runs_all_six_stage_actions() -> None:
    fake = FakeMCP()
    lifecycle = runner.OneChapterLifecycle(
        mcp_url="http://mcp.example/mcp",
        call_tool=fake.call,
        fault_id=FAULT_ID,
    )

    project = asyncio.run(lifecycle.create_genesis_project())
    task = asyncio.run(lifecycle.start_writing(project.project_id))

    assert project.project_id == PROJECT_ID
    assert task.task_id == TASK_ID
    writes = {
        "genesis_stage_generate",
        "genesis_stage_refine",
        "genesis_stage_lock",
    }
    for stage in runner.GENESIS_STAGES:
        stage_calls = [
            (index, name, args)
            for index, (name, args) in enumerate(fake.calls)
            if args.get("stage_key") == stage
        ]
        assert [name for _, name, _ in stage_calls] == [
            "genesis_stage_generate",
            "genesis_stage_refine",
            "genesis_stage_lock",
        ]
        for index, name, _ in stage_calls:
            assert name in writes
            assert fake.calls[index - 1][0] == "genesis_get"

    active_index = next(
        index
        for index, (name, _) in enumerate(fake.calls)
        if name == "task_active_generation_check"
    )
    start_index = next(
        index
        for index, (name, _) in enumerate(fake.calls)
        if name == "project_start_writing"
    )
    assert active_index < start_index
    assert fake.calls[start_index][1] == {
        "project_id": PROJECT_ID,
        "auto_continue": False,
        "max_chapters": 1,
    }
    assert not any(
        name
        in {
            "project_update",
            "project_continue_generation",
            "chapter_review_retry",
        }
        for name, _ in fake.calls
    )


def test_mcp_fixture_blocks_writing_when_active_generation_exists() -> None:
    fake = FakeMCP(active=True)
    lifecycle = runner.OneChapterLifecycle(
        mcp_url="http://mcp.example/mcp",
        call_tool=fake.call,
        fault_id=FAULT_ID,
    )

    with pytest.raises(runner.SetupBlocked, match="active generation task"):
        asyncio.run(lifecycle.start_writing(PROJECT_ID))

    assert not any(name == "project_start_writing" for name, _ in fake.calls)


def raw_canon() -> dict[str, Any]:
    return {
        "canon_id": CANON_ID,
        "natural_key": "canon-idempotency-a",
        "project_id": PROJECT_ID,
        "chapter_id": CHAPTER_ID,
        "canon_version": 1,
        "content_sha256": BODY_SHA,
        "created_at": "ignored",
        "status": "committed",
    }


def raw_outbox(attempts: int) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "canon_commit_id": CANON_ID,
        "canon_idempotency_key": "canon-idempotency-a",
        "project_id": PROJECT_ID,
        "chapter_number": 1,
        "candidate_id": CANDIDATE_ID,
        "trigger": "canon_commit",
    }
    return {
        "event_id": "event-a",
        "aggregate_type": "project",
        "aggregate_id": PROJECT_ID,
        "event_type": "canon.projection.requested",
        "payload_json": json.dumps(payload),
        "attempts": attempts,
        "status": "pending",
        "updated_at": "ignored",
    }


def test_sql_collectors_normalize_canon_and_durable_outbox_identity() -> None:
    canon = runner.normalize_canon_records([raw_canon()])
    outbox = runner.normalize_outbox_record(raw_outbox(3))

    assert canon == [
        {
            "canon_id": CANON_ID,
            "natural_key": "canon-idempotency-a",
            "project_id": PROJECT_ID,
            "chapter_id": CHAPTER_ID,
            "canon_version": 1,
            "content_sha256": BODY_SHA,
        }
    ]
    assert outbox == {
        "event_id": "event-a",
        "aggregate_type": "canon",
        "aggregate_id": CANON_ID,
        "event_type": "canon.projection.requested",
        "idempotency_key": "canon-idempotency-a",
        "payload_sha256": evidence.stable_hash(
            json.loads(raw_outbox(3)["payload_json"])
        ),
        "attempt": 3,
    }
    assert "status" not in outbox
    assert "updated_at" not in outbox


def projection_status() -> dict[str, Any]:
    return {
        "project_id": PROJECT_ID,
        "status": "healthy",
        "healthy": True,
        "target_canon_commit_id": CANON_ID,
        "target_chapter_number": 1,
        "components": [
            {
                "projection_kind": "llm_kb",
                "status": "healthy",
                "healthy": True,
                "target_canon_commit_id": CANON_ID,
                "target_chapter_number": 1,
                "projected_canon_commit_id": CANON_ID,
                "projected_chapter_number": 1,
            }
        ],
    }


def test_qdrant_collector_requires_project_bound_points_and_healthy_checkpoint() -> None:
    points = [
        {
            "id": "point-a",
            "payload": {
                "project_id": PROJECT_ID,
                "index_kind": "llm_kb",
                "as_of_chapter": 1,
            },
        },
        {
            "id": "foreign-point",
            "payload": {
                "project_id": "other-project",
                "index_kind": "llm_kb",
                "as_of_chapter": 1,
            },
        },
    ]

    projections, identities = runner.normalize_qdrant_projection(
        status=projection_status(),
        points=points,
        project_id=PROJECT_ID,
        canon_id=CANON_ID,
    )

    assert projections == [
        {
            "projection_type": "llm_kb",
            "identity_id": "point-a",
            "canon_id": CANON_ID,
            "status": "converged",
        }
    ]
    assert identities == [
        {
            "collection": "canon",
            "projection_type": "llm_kb",
            "point_id": "point-a",
            "canon_id": CANON_ID,
        }
    ]

    degraded = projection_status()
    degraded["components"][0]["healthy"] = False
    with pytest.raises(runner.SetupBlocked, match="not converged"):
        runner.normalize_qdrant_projection(
            status=degraded,
            points=points,
            project_id=PROJECT_ID,
            canon_id=CANON_ID,
        )


def test_projection_identity_collector_binds_api_status_to_sql_identity() -> None:
    rows = [
        {
            "projection_id": "checkpoint-a",
            "projection_type": "llm_kb",
            "project_id": PROJECT_ID,
            "projected_canon_commit_id": CANON_ID,
        }
    ]

    projections, identities = runner.normalize_projection_identities(
        status=projection_status(),
        rows=rows,
        project_id=PROJECT_ID,
        canon_id=CANON_ID,
    )

    assert projections == [
        {
            "projection_type": "llm_kb",
            "identity_id": "checkpoint-a",
            "canon_id": CANON_ID,
            "status": "converged",
        }
    ]
    assert identities == [
        {
            "projection_type": "llm_kb",
            "projection_id": "checkpoint-a",
            "canon_id": CANON_ID,
        }
    ]


class FakeCompleted:
    def __init__(
        self, args: list[str], *, returncode: int = 0, stdout: str = "{}"
    ) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_controller_constructs_only_recovery_stack_commands(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, str], Path]] = []

    def execute(
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        **_kwargs: Any,
    ) -> FakeCompleted:
        calls.append((list(args), dict(env), cwd))
        return FakeCompleted(args)

    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}", encoding="utf-8")
    evidence_dir = (tmp_path / "evidence").resolve()
    controller = runner.RecoveryController(
        candidate_manifest=candidate,
        evidence_dir=evidence_dir,
        execute=execute,
        python_executable="/python",
    )

    controller.fresh_up(FAULT_ID)
    controller.kill("generation-worker", FAULT_ID)
    controller.stop("qdrant", FAULT_ID)
    controller.start("qdrant", FAULT_ID)
    controller.snapshot("after")
    controller.destroy()

    expected = [
        ["fresh-up", "--fault-id", FAULT_ID],
        ["kill", "generation-worker", "--fault-id", FAULT_ID],
        ["stop", "qdrant", "--fault-id", FAULT_ID],
        ["start", "qdrant", "--fault-id", FAULT_ID],
        ["snapshot", "--label", "after"],
        ["destroy"],
    ]
    for (argv, env, cwd), suffix in zip(calls, expected):
        assert argv[:2] == ["/python", str(runner.CONTROLLER_PATH)]
        assert argv[2:] == suffix
        assert cwd == runner.ROOT
        assert env["FORWIN_RECOVERY_CANDIDATE_MANIFEST"] == str(
            candidate.resolve()
        )
        assert env["FORWIN_RECOVERY_EVIDENCE_DIR"] == str(evidence_dir)
        assert argv[0] != "docker"


def token(kind: str, label: str) -> str:
    return f"{kind}-{label}"


def valid_projection_snapshots(
    kind: str = "projection_consumer_unavailable",
) -> dict[str, dict[str, Any]]:
    fixture = {
        "fixture_id": token(kind, "fixture"),
        "fault_id": FAULT_ID,
        "resource_type": "chapter",
        "resource_id": CHAPTER_ID,
    }
    canon = runner.normalize_canon_records([raw_canon()])
    snapshots = {
        stage: runner.snapshot_envelope(
            source_sha=SOURCE_SHA,
            fault_kind=kind,
            fault_id=FAULT_ID,
            stage=stage,
            fixture=fixture,
        )
        for stage in evidence.STAGES
    }
    for stage, snapshot in snapshots.items():
        snapshot["state"]["database"]["canon_commits"] = copy.deepcopy(canon)
        snapshot["state"]["database"]["outbox"] = runner.normalize_outbox_record(
            raw_outbox(0 if stage != "after" else 1)
        )
    status = projection_status()
    projections, identities = runner.normalize_projection_identities(
        status=status,
        rows=[
            {
                "projection_id": "checkpoint-a",
                "projection_type": "llm_kb",
                "project_id": PROJECT_ID,
                "projected_canon_commit_id": CANON_ID,
            }
        ],
        project_id=PROJECT_ID,
        canon_id=CANON_ID,
    )
    snapshots["after"]["state"]["external"].update(
        projections=projections,
        projection_identities=identities,
    )
    return snapshots


DESTROY_SERVICES = {
    service: {"exists": False, "running": False}
    for service in finalizer.DESTROY_SERVICES
}


def append_event(
    events: list[dict[str, Any]],
    *,
    action: str,
    recorded_at: str,
    identity: dict[str, Any],
    run_identity: dict[str, str],
    volume: dict[str, Any],
    **payload: Any,
) -> None:
    event = {
        "schema_version": 2,
        "recorded_at": recorded_at,
        "action": action,
        "fault_id": FAULT_ID,
        "identity": copy.deepcopy(identity),
        "run_identity": copy.deepcopy(run_identity),
        "database_volume": copy.deepcopy(volume),
        "previous_event_sha256": (
            events[-1]["event_sha256"] if events else "0" * 64
        ),
        **payload,
    }
    event["event_sha256"] = finalizer.event_hash(event)
    events.append(event)


def valid_event_log(evidence_dir: Path) -> Path:
    run_identity = {
        "run_id": "1" * 32,
        "evidence_directory": str(evidence_dir.resolve()),
        "database_volume_name": (
            f"forwin-v5-recovery-{'1' * 32}-postgres-data"
        ),
    }
    created_at = "2026-07-26T12:00:00+00:00"
    volume = {
        "name": run_identity["database_volume_name"],
        "exists": True,
        "created_at": created_at,
        "fingerprint": evidence.stable_hash(
            {
                "created_at": created_at,
                "name": run_identity["database_volume_name"],
            }
        ),
    }
    absent = {
        "name": run_identity["database_volume_name"],
        "exists": False,
    }
    identity = {"source_sha": SOURCE_SHA}
    events: list[dict[str, Any]] = []
    append_event(
        events,
        action="fresh_up_started",
        recorded_at="2026-07-26T11:59:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=absent,
    )
    append_event(
        events,
        action="fresh_up_completed",
        recorded_at="2026-07-26T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=volume,
    )
    append_event(
        events,
        action="fault_service_stopped",
        recorded_at="2026-07-26T12:01:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=volume,
        service="outbox-worker",
        fault_time="2026-07-26T12:01:00+00:00",
    )
    append_event(
        events,
        action="fault_service_recovered",
        recorded_at="2026-07-26T12:02:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=volume,
        service="outbox-worker",
        recovery_time="2026-07-26T12:02:00+00:00",
    )
    append_event(
        events,
        action="destroyed",
        recorded_at="2026-07-26T12:03:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=absent,
        database_volume_before=volume,
        after={"services": copy.deepcopy(DESTROY_SERVICES)},
    )
    path = evidence_dir / "stack-events.jsonl"
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def test_writer_derives_before_write_then_reopens_hashes_and_revalidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "evidence").resolve()
    evidence_dir.mkdir()
    events = valid_event_log(evidence_dir)
    snapshots = valid_projection_snapshots()
    derive_calls = 0
    original_derive = evidence.derive_assertions

    def count_derive(kind: str, values: dict[str, dict[str, Any]]) -> dict[str, Any]:
        nonlocal derive_calls
        derive_calls += 1
        return original_derive(kind, values)

    monkeypatch.setattr(evidence, "derive_assertions", count_derive)
    writer = runner.EvidenceWriter(
        evidence_dir=evidence_dir,
        evaluator=evidence,
        report_validator=finalizer.fault_report_violations,
    )

    report_path = writer.write_pass_report(
        fault_kind="projection_consumer_unavailable",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        snapshots=snapshots,
        event_log_path=events,
    )

    assert derive_calls >= 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["result"] == "pass"
    assert report["assertions"] == original_derive(
        "projection_consumer_unavailable",
        snapshots,
    )
    assert report["event_log"]["path"] == str(events)
    assert report["event_log"]["sha256"] == runner.sha256_file(events)
    assert [Path(item["path"]).parent for item in report["artifacts"]] == [
        evidence_dir,
        evidence_dir,
        evidence_dir,
    ]
    for artifact in report["artifacts"]:
        path = Path(artifact["path"])
        assert path.is_file()
        assert artifact["sha256"] == runner.sha256_file(path)
    assert finalizer.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    ) == []


def test_writer_is_atomic_no_clobber_for_snapshots_and_report(
    tmp_path: Path,
) -> None:
    evidence_dir = (tmp_path / "evidence").resolve()
    evidence_dir.mkdir()
    existing = evidence_dir / "before.json"
    existing.write_text('{"sentinel":true}\n', encoding="utf-8")
    events = valid_event_log(evidence_dir)
    writer = runner.EvidenceWriter(
        evidence_dir=evidence_dir,
        evaluator=evidence,
        report_validator=finalizer.fault_report_violations,
    )

    with pytest.raises(runner.RunnerError, match="already exists"):
        writer.write_pass_report(
            fault_kind="projection_consumer_unavailable",
            fault_id=FAULT_ID,
            source_sha=SOURCE_SHA,
            snapshots=valid_projection_snapshots(),
            event_log_path=events,
        )

    assert existing.read_text(encoding="utf-8") == '{"sentinel":true}\n'
    assert not list(evidence_dir.glob(".*.tmp-*"))


def test_writer_rejects_reopened_evaluator_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "evidence").resolve()
    evidence_dir.mkdir()
    events = valid_event_log(evidence_dir)
    original = evidence.derive_assertions
    calls = 0

    def mismatch(kind: str, values: dict[str, dict[str, Any]]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        derived = original(kind, values)
        if calls > 1:
            derived["projection_converged"] = False
        return derived

    monkeypatch.setattr(evidence, "derive_assertions", mismatch)
    writer = runner.EvidenceWriter(
        evidence_dir=evidence_dir,
        evaluator=evidence,
        report_validator=finalizer.fault_report_violations,
    )

    with pytest.raises(runner.RunnerError, match="reopened evaluator mismatch"):
        writer.write_pass_report(
            fault_kind="projection_consumer_unavailable",
            fault_id=FAULT_ID,
            source_sha=SOURCE_SHA,
            snapshots=valid_projection_snapshots(),
            event_log_path=events,
        )

    assert not (evidence_dir / "fault-report.json").exists()


def test_setup_blocked_report_is_atomic_and_cannot_validate_as_pass(
    tmp_path: Path,
) -> None:
    evidence_dir = (tmp_path / "evidence").resolve()
    writer = runner.EvidenceWriter(
        evidence_dir=evidence_dir,
        evaluator=evidence,
        report_validator=finalizer.fault_report_violations,
    )

    path = writer.write_setup_blocked(
        fault_kind="generation_worker_precommit_crash",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        failure_stage="barrier_wait",
        failure_reason="required waiter not observed",
        cleanup_errors=[],
    )

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert report["result"] == "setup_blocked"
    assert report["failure_stage"] == "barrier_wait"
    assert finalizer.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    )
    with pytest.raises(runner.RunnerError, match="already exists"):
        writer.write_setup_blocked(
            fault_kind="generation_worker_precommit_crash",
            fault_id=FAULT_ID,
            source_sha=SOURCE_SHA,
            failure_stage="again",
            failure_reason="must not clobber",
            cleanup_errors=[],
        )


def test_setup_blocked_report_rejects_byte_change_while_reopening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = (tmp_path / "evidence").resolve()
    writer = runner.EvidenceWriter(
        evidence_dir=evidence_dir,
        evaluator=evidence,
        report_validator=finalizer.fault_report_violations,
    )
    original_load = writer._load_json

    def rewrite_before_load(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return original_load(path)

    monkeypatch.setattr(writer, "_load_json", rewrite_before_load)

    with pytest.raises(
        runner.RunnerError,
        match="setup_blocked report changed while reopening",
    ):
        writer.write_setup_blocked(
            fault_kind="generation_worker_precommit_crash",
            fault_id=FAULT_ID,
            source_sha=SOURCE_SHA,
            failure_stage="barrier_wait",
            failure_reason="required waiter not observed",
            cleanup_errors=[],
        )


class FakeRows:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def fetch_all(
        self,
        statement: str,
        params: tuple[Any, ...] | list[Any] = (),
    ) -> list[dict[str, Any]]:
        values = tuple(params)
        self.calls.append((statement, values))
        for marker, rows in self.rows.items():
            if marker in statement:
                return copy.deepcopy(rows)
        raise AssertionError(f"unexpected SQL: {statement}")


def fixture_context() -> Any:
    return runner.FixtureContext(
        fixture_id="fixture-a",
        fault_id=FAULT_ID,
        project_id=PROJECT_ID,
        chapter_number=1,
        chapter_id=CHAPTER_ID,
        task_id=TASK_ID,
        candidate_id=CANDIDATE_ID,
    )


def test_sql_collector_binds_fixture_identity_and_builds_generation_snapshot() -> None:
    source = FakeRows(
        {
            "/* task4 task */": [
                {
                    "task_id": TASK_ID,
                    "lease_epoch": 7,
                    "project_id": PROJECT_ID,
                }
            ],
            "/* task4 canon */": [raw_canon()],
        }
    )
    collector = runner.SQLCollector(source)

    snapshot = collector.generation_snapshot(
        source_sha=SOURCE_SHA,
        fault_kind="generation_worker_precommit_crash",
        stage="after",
        fixture=fixture_context(),
    )

    assert snapshot["state"]["database"]["task"] == {
        "task_id": TASK_ID,
        "lease_epoch": 7,
    }
    assert snapshot["state"]["database"]["canon_commits"][0]["canon_id"] == CANON_ID
    assert snapshot["state"]["database"]["authoritative_identities"] == [
        {
            "entity_type": "canon",
            "record_id": CANON_ID,
            "project_id": PROJECT_ID,
            "chapter_id": CHAPTER_ID,
            "natural_key": "canon-idempotency-a",
        }
    ]
    assert {(params) for _, params in source.calls} == {
        (TASK_ID, PROJECT_ID),
        (PROJECT_ID, 1),
    }
    assert evidence.snapshot_violations(
        "generation_worker_precommit_crash",
        {
            "before": {
                **snapshot,
                "stage": "before",
                "state": {
                    **snapshot["state"],
                    "database": {
                        "task": snapshot["state"]["database"]["task"],
                    },
                },
            },
            "during": {
                **snapshot,
                "stage": "during",
                "state": {
                    **snapshot["state"],
                    "database": {
                        "task": snapshot["state"]["database"]["task"],
                        "canon_commits": [],
                    },
                },
            },
            "after": snapshot,
        },
    ) == []


def test_sql_collector_normalizes_projection_outbox_and_checkpoint_rows() -> None:
    source = FakeRows(
        {
            "/* task4 canon */": [raw_canon()],
            "/* task4 projection outbox */": [raw_outbox(2)],
            "/* task4 projection identities */": [
                {
                    "projection_id": "checkpoint-a",
                    "projection_type": "llm_kb",
                    "project_id": PROJECT_ID,
                    "projected_canon_commit_id": CANON_ID,
                }
            ],
        }
    )
    collector = runner.SQLCollector(source)
    fixture = fixture_context()

    snapshot = collector.projection_snapshot(
        source_sha=SOURCE_SHA,
        fault_kind="projection_consumer_unavailable",
        stage="during",
        fixture=fixture,
    )
    identities = collector.projection_identity_rows(fixture)

    assert snapshot["state"]["database"]["canon_commits"][0]["canon_id"] == CANON_ID
    assert snapshot["state"]["database"]["outbox"]["attempt"] == 2
    assert identities[0]["projection_id"] == "checkpoint-a"
    assert all(
        params == (PROJECT_ID, 1)
        for _, params in source.calls[:2]
    )
    assert source.calls[2][1] == (PROJECT_ID,)


def test_sql_collector_wait_boundaries_fail_closed() -> None:
    source = FakeRows(
        {
            "/* task4 fixture boundary */": [
                {
                    "chapter_id": CHAPTER_ID,
                    "candidate_id": CANDIDATE_ID,
                    "candidate_status": "drafted",
                    "task_id": TASK_ID,
                }
            ]
        }
    )
    collector = runner.SQLCollector(source)

    with pytest.raises(runner.SetupBlocked, match="review-ready"):
        collector.wait_review_ready(
            fault_id=FAULT_ID,
            fixture_id="fixture-a",
            project_id=PROJECT_ID,
            task_id=TASK_ID,
            timeout_seconds=0,
        )

    assert source.calls == [
        (
            runner.FIXTURE_BOUNDARY_SQL,
            (PROJECT_ID, 1, TASK_ID),
        )
    ]


def test_api_client_uses_supported_approve_status_and_refresh_paths() -> None:
    calls: list[
        tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]
    ] = []

    def transport(
        method: str,
        url: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append(
            (
                method,
                url,
                copy.deepcopy(query),
                copy.deepcopy(json_body),
            )
        )
        if url.endswith("/review/approve"):
            return {
                "ok": True,
                "project_id": PROJECT_ID,
                "chapter_number": 1,
                "status": "accepted",
            }
        if url.endswith("/projections/status"):
            return projection_status()
        return {
            "ok": True,
            "project_id": PROJECT_ID,
            "target_canon_commit_id": CANON_ID,
        }

    api = runner.ForWinAPI(
        api_url="https://api.example/base",
        transport=transport,
    )

    approved = api.approve_chapter(PROJECT_ID, 1)
    status = api.projection_status(PROJECT_ID)
    refreshed = api.refresh_projection(PROJECT_ID, 1)

    assert approved["status"] == "accepted"
    assert status["target_canon_commit_id"] == CANON_ID
    assert refreshed["ok"] is True
    assert calls == [
        (
            "POST",
            f"https://api.example/base/api/projects/{PROJECT_ID}/chapters/1/review/approve",
            None,
            {
                "continue_generation": False,
                "reason": "fault-local recovery evidence acceptance",
            },
        ),
        (
            "GET",
            f"https://api.example/base/api/projects/{PROJECT_ID}/projections/status",
            None,
            None,
        ),
        (
            "POST",
            f"https://api.example/base/api/projects/{PROJECT_ID}/projections/refresh",
            {
                "projection_kind": "all",
                "as_of_chapter": 1,
                "force": True,
                "defer": False,
            },
            None,
        ),
    ]


def test_qdrant_reader_scrolls_parameterized_endpoint_and_collection() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def transport(
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        body = copy.deepcopy(json_body or {})
        calls.append((method, url, body))
        if len(calls) == 1:
            return {
                "result": {
                    "points": [
                        {
                            "id": "point-a",
                            "payload": {
                                "project_id": PROJECT_ID,
                                "index_kind": "llm_kb",
                            },
                        }
                    ],
                    "next_page_offset": "next-a",
                }
            }
        return {
            "result": {
                "points": [
                    {
                        "id": "point-b",
                        "payload": {
                            "project_id": PROJECT_ID,
                            "index_kind": "llm_kb",
                        },
                    }
                ],
                "next_page_offset": None,
            }
        }

    qdrant = runner.QdrantReader(
        qdrant_url="https://vectors.example",
        collection="fixture-vectors",
        transport=transport,
    )

    points = qdrant.project_points(PROJECT_ID)

    assert [point["id"] for point in points] == ["point-a", "point-b"]
    assert all(
        url
        == "https://vectors.example/collections/fixture-vectors/points/scroll"
        for _, url, _ in calls
    )
    assert calls[0][2]["filter"] == {
        "must": [
            {
                "key": "project_id",
                "match": {"value": PROJECT_ID},
            }
        ]
    }
    assert "offset" not in calls[0][2]
    assert calls[1][2]["offset"] == "next-a"


class FakeControllerLifecycle:
    def __init__(self, evidence_dir: Path) -> None:
        self.evidence_dir = evidence_dir
        self.event_log_path = evidence_dir / runner.EVENT_LOG_NAME
        self.calls: list[tuple[str, ...]] = []

    def fresh_up(self, fault_id: str) -> None:
        self.calls.append(("fresh_up", fault_id))

    def kill(self, service: str, fault_id: str) -> None:
        self.calls.append(("kill", service, fault_id))

    def stop(self, service: str, fault_id: str) -> None:
        self.calls.append(("stop", service, fault_id))

    def start(self, service: str, fault_id: str) -> None:
        self.calls.append(("start", service, fault_id))

    def destroy(self) -> None:
        self.calls.append(("destroy",))


class FakeLifecycleProject:
    async def create_genesis_project(self) -> Any:
        return runner.ProjectFixture(PROJECT_ID)

    async def start_writing(self, project_id: str) -> Any:
        return runner.TaskFixture(project_id, TASK_ID)


class MissingBarrier:
    def __init__(self, log: list[str] | None = None) -> None:
        self.cleaned = False
        self.log = log

    def install(self, **_kwargs: Any) -> None:
        return None

    def wait_for_blocked_waiter(self) -> None:
        raise runner.SetupBlocked("required waiter not observed")

    def cleanup(self) -> None:
        self.cleaned = True
        if self.log is not None:
            self.log.append("barrier_cleanup")


class FakeSetupWriter:
    def __init__(self, evidence_dir: Path) -> None:
        self.evidence_dir = evidence_dir
        self.payload: dict[str, Any] | None = None

    def write_setup_blocked(self, **payload: Any) -> Path:
        self.payload = payload
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / runner.REPORT_NAME
        path.write_text(
            json.dumps({"result": "setup_blocked", **payload}),
            encoding="utf-8",
        )
        return path


class FakePassWriter(FakeSetupWriter):
    def __init__(self, evidence_dir: Path, log: list[str] | None = None) -> None:
        super().__init__(evidence_dir)
        self.pass_payload: dict[str, Any] | None = None
        self.log = log

    def write_pass_report(self, **payload: Any) -> Path:
        self.pass_payload = payload
        if self.log is not None:
            self.log.append("write_pass")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / runner.REPORT_NAME
        path.write_text('{"result":"pass"}', encoding="utf-8")
        return path


class FakeBoundaryCollector:
    def wait_task_fixture(self, **_kwargs: Any) -> Any:
        return fixture_context()

    def generation_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "stage": kwargs["stage"],
            "state": {
                "database": {
                    "task": {
                        "task_id": TASK_ID,
                        "lease_epoch": 1,
                    }
                }
            },
        }


def test_live_runner_missing_barrier_is_setup_blocked_and_cleans_in_finally(
    tmp_path: Path,
) -> None:
    evidence_dir = (tmp_path / "evidence").resolve()
    log: list[str] = []
    controller = LoggingController(evidence_dir, log)
    barrier = MissingBarrier(log)
    writer = FakeSetupWriter(evidence_dir)
    live = runner.LiveRunner(
        fault_kind="generation_worker_precommit_crash",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        controller=controller,
        lifecycle=FakeLifecycleProject(),
        sql_collector=FakeBoundaryCollector(),
        api=None,
        qdrant=None,
        writer=writer,
        barrier_factory=lambda: barrier,
    )

    result = live.run()

    assert result.status == "setup_blocked"
    assert barrier.cleaned is True
    assert writer.payload is not None
    assert writer.payload["failure_stage"] == "barrier_wait"
    assert controller.calls == [
        ("fresh_up", FAULT_ID),
        ("stop", "generation-worker", FAULT_ID),
        ("start", "generation-worker", FAULT_ID),
        ("destroy",),
    ]
    assert not any(call[0] == "kill" for call in controller.calls)
    assert (
        log.index("stop:generation-worker")
        < log.index("barrier_cleanup")
        < log.index("start:generation-worker")
    )


class SuccessfulBarrier:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.cleanup_count = 0

    def install(self, **_kwargs: Any) -> None:
        self.log.append("barrier_install")

    def wait_for_blocked_waiter(self) -> Any:
        self.log.append("barrier_wait")
        return runner.BarrierObservation(holder_pid=100, waiter_pid=200)

    def cleanup(self) -> None:
        self.cleanup_count += 1
        self.log.append("barrier_cleanup")


class SuccessfulGenerationCollector:
    def wait_task_fixture(self, **_kwargs: Any) -> Any:
        return fixture_context()

    def generation_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        stage = kwargs["stage"]
        return {
            "stage": stage,
            "state": {
                "database": {
                    "task": {
                        "task_id": TASK_ID,
                        "lease_epoch": 5 if stage == "after" else 4,
                    }
                }
            },
        }

    def wait_task_reclaimed(self, fixture: Any, **_kwargs: Any) -> Any:
        return fixture


class LoggingController(FakeControllerLifecycle):
    def __init__(self, evidence_dir: Path, log: list[str]) -> None:
        super().__init__(evidence_dir)
        self.log = log

    def fresh_up(self, fault_id: str) -> None:
        super().fresh_up(fault_id)
        self.log.append("fresh_up")

    def kill(self, service: str, fault_id: str) -> None:
        super().kill(service, fault_id)
        self.log.append(f"kill:{service}")

    def stop(self, service: str, fault_id: str) -> None:
        super().stop(service, fault_id)
        self.log.append(f"stop:{service}")

    def start(self, service: str, fault_id: str) -> None:
        super().start(service, fault_id)
        self.log.append(f"start:{service}")

    def destroy(self) -> None:
        super().destroy()
        self.log.append("destroy")


def test_live_generation_success_cleans_barrier_before_worker_recovery_and_report(
    tmp_path: Path,
) -> None:
    log: list[str] = []
    evidence_dir = (tmp_path / "evidence").resolve()
    controller = LoggingController(evidence_dir, log)
    barrier = SuccessfulBarrier(log)
    writer = FakePassWriter(evidence_dir, log)
    live = runner.LiveRunner(
        fault_kind="generation_worker_precommit_crash",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        controller=controller,
        lifecycle=FakeLifecycleProject(),
        sql_collector=SuccessfulGenerationCollector(),
        api=None,
        qdrant=None,
        writer=writer,
        barrier_factory=lambda: barrier,
    )

    result = live.run()

    assert result.status == "pass"
    assert writer.pass_payload is not None
    assert set(writer.pass_payload["snapshots"]) == {
        "before",
        "during",
        "after",
    }
    barrier_evidence = writer.pass_payload["supplemental_artifacts"][
        "barrier-observation.json"
    ]
    assert barrier_evidence["holder_pid"] == 100
    assert barrier_evidence["waiter_pid"] == 200
    assert barrier_evidence["residue_count"] == 0
    assert log.index("barrier_wait") < log.index("kill:generation-worker")
    assert log.index("kill:generation-worker") < log.index("barrier_cleanup")
    assert log.index("barrier_cleanup") < log.index("start:generation-worker")
    assert log.index("destroy") < log.index("write_pass")


class SuccessfulProjectionCollector:
    def wait_review_ready(self, **_kwargs: Any) -> Any:
        return fixture_context()

    def wait_projection_base(self, fixture: Any) -> Any:
        return runner.FixtureContext(
            fixture_id=fixture.fixture_id,
            fault_id=fixture.fault_id,
            project_id=fixture.project_id,
            chapter_number=fixture.chapter_number,
            chapter_id=fixture.chapter_id,
            task_id=fixture.task_id,
            candidate_id=fixture.candidate_id,
            canon_id=CANON_ID,
        )

    def projection_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "stage": kwargs["stage"],
            "state": {"external": {}},
        }

    def wait_outbox_processed(self, _fixture: Any) -> None:
        return None

    def projection_identity_rows(self, _fixture: Any) -> list[dict[str, Any]]:
        return [
            {
                "projection_id": "checkpoint-a",
                "projection_type": "llm_kb",
                "project_id": PROJECT_ID,
                "projected_canon_commit_id": CANON_ID,
            }
        ]


class SuccessfulAPI:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def approve_chapter(self, _project_id: str, _chapter_number: int) -> None:
        self.log.append("approve")

    def wait_projection_converged(
        self, _project_id: str, _canon_id: str
    ) -> dict[str, Any]:
        self.log.append("converged")
        return projection_status()

    def refresh_projection(
        self, _project_id: str, _chapter_number: int
    ) -> None:
        self.log.append("refresh")


class SuccessfulQdrant:
    def project_points(self, _project_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "point-a",
                "payload": {
                    "project_id": PROJECT_ID,
                    "index_kind": "llm_kb",
                },
            }
        ]


@pytest.mark.parametrize(
    ("kind", "service"),
    [
        ("qdrant_unavailable", "qdrant"),
        ("projection_consumer_unavailable", "outbox-worker"),
    ],
)
def test_live_projection_success_uses_supported_accept_recover_and_refresh_order(
    tmp_path: Path,
    kind: str,
    service: str,
) -> None:
    log: list[str] = []
    evidence_dir = (tmp_path / kind).resolve()
    controller = LoggingController(evidence_dir, log)
    writer = FakePassWriter(evidence_dir, log)
    live = runner.LiveRunner(
        fault_kind=kind,
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        controller=controller,
        lifecycle=FakeLifecycleProject(),
        sql_collector=SuccessfulProjectionCollector(),
        api=SuccessfulAPI(log),
        qdrant=SuccessfulQdrant() if kind == "qdrant_unavailable" else None,
        writer=writer,
    )

    result = live.run()

    assert result.status == "pass"
    assert log.index(f"stop:{service}") < log.index("approve")
    assert log.index("approve") < log.index(f"start:{service}")
    convergences = [index for index, value in enumerate(log) if value == "converged"]
    assert len(convergences) == 2
    assert log.index(f"start:{service}") < convergences[0]
    assert convergences[0] < log.index("refresh") < convergences[1]
    assert log.index("destroy") < log.index("write_pass")
    assert writer.pass_payload is not None
    after = writer.pass_payload["snapshots"]["after"]
    identity_key = (
        "point_identities"
        if kind == "qdrant_unavailable"
        else "projection_identities"
    )
    assert after["state"]["external"][identity_key]


def test_writer_hashes_reopens_and_binds_supplemental_artifacts(
    tmp_path: Path,
) -> None:
    evidence_dir = (tmp_path / "evidence").resolve()
    evidence_dir.mkdir()
    events = valid_event_log(evidence_dir)
    writer = runner.EvidenceWriter(
        evidence_dir=evidence_dir,
        evaluator=evidence,
        report_validator=finalizer.fault_report_violations,
    )
    supplemental = {
        "barrier-observation.json": {
            "fault_id": FAULT_ID,
            "holder_pid": 100,
            "waiter_pid": 200,
            "residue_count": 0,
        }
    }

    report_path = writer.write_pass_report(
        fault_kind="projection_consumer_unavailable",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        snapshots=valid_projection_snapshots(),
        event_log_path=events,
        supplemental_artifacts=supplemental,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    artifact = report["supplemental_artifacts"][0]
    assert artifact["name"] == "barrier-observation.json"
    assert Path(artifact["path"]).parent == evidence_dir
    assert artifact["sha256"] == runner.sha256_file(Path(artifact["path"]))
    assert json.loads(Path(artifact["path"]).read_text(encoding="utf-8")) == (
        supplemental["barrier-observation.json"]
    )


def test_run_config_uses_cli_and_environment_for_every_live_endpoint(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps({"source": {"sha": SOURCE_SHA}}),
        encoding="utf-8",
    )
    arguments = runner.parse_args(
        [
            "run",
            "--fault-kind",
            "qdrant_unavailable",
            "--fault-id",
            FAULT_ID,
            "--candidate-manifest",
            str(candidate),
            "--mcp-url",
            "https://mcp.example/mcp",
            "--api-url",
            "https://api.example",
            "--database-url-env",
            "FIXTURE_DATABASE_URL",
            "--evidence-dir",
            str(tmp_path / "evidence"),
        ]
    )
    environment = {
        "FIXTURE_DATABASE_URL": "postgresql://db.example/fixture",
        "FORWIN_RECOVERY_QDRANT_URL": "https://qdrant.example",
        "FORWIN_RECOVERY_QDRANT_COLLECTION": "fixture-collection",
    }

    config = runner.resolve_run_config(arguments, environ=environment)

    assert config.database_url == "postgresql://db.example/fixture"
    assert config.mcp_url == "https://mcp.example/mcp"
    assert config.api_url == "https://api.example"
    assert config.qdrant_url == "https://qdrant.example"
    assert config.qdrant_collection == "fixture-collection"
    assert config.evidence_dir == (tmp_path / "evidence").resolve()

    del environment["FORWIN_RECOVERY_QDRANT_URL"]
    with pytest.raises(runner.RunnerError, match="FORWIN_RECOVERY_QDRANT_URL"):
        runner.resolve_run_config(arguments, environ=environment)


def test_cli_accepts_only_the_four_task4_fault_kinds(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps({"source": {"sha": SOURCE_SHA}}),
        encoding="utf-8",
    )
    arguments = [
        "run",
        "--fault-kind",
        "qdrant_unavailable",
        "--fault-id",
        FAULT_ID,
        "--candidate-manifest",
        str(candidate),
        "--mcp-url",
        "http://mcp.example/mcp",
        "--api-url",
        "http://api.example",
        "--database-url-env",
        "TASK4_DATABASE_URL",
        "--evidence-dir",
        str(tmp_path / "evidence"),
    ]

    parsed = runner.parse_args(arguments)

    assert parsed.command == "run"
    assert parsed.fault_kind == "qdrant_unavailable"
    assert set(runner.SUPPORTED_FAULTS) == {
        "generation_worker_precommit_crash",
        "generation_worker_postcommit_crash",
        "qdrant_unavailable",
        "projection_consumer_unavailable",
    }
    with pytest.raises(SystemExit):
        runner.parse_args(
            [
                *arguments[:2],
                "publisher_backend_unavailable",
                *arguments[3:],
            ]
        )
