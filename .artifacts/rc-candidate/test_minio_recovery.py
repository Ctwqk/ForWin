from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


MODULE_PATH = Path(__file__).with_name("minio_recovery.py")
REPO_ROOT = MODULE_PATH.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
COMMON_PATH = Path(__file__).with_name("recovery_runner_common.py")
EVALUATOR_PATH = Path(__file__).with_name("recovery_evidence.py")
FINALIZER_PATH = Path(__file__).with_name("finalize_recovery.py")
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
FAULT_ID = "task5-fault-a"
PROJECT_ID = "project-a"
CHAPTER_ID = "chapter-a"
CANDIDATE_ID = "candidate-a"
CANON_ID = "canon-a"
TASK_ID = "task-a"
CANON_KEY = "canon-natural-a"
BODY = json.dumps(
    {
        "schema_version": "post-canon-trace-v1",
        "project_id": PROJECT_ID,
        "canon_commit_id": CANON_ID,
        "chapter_number": 1,
        "step_name": "world",
        "attempts": [{"attempt_no": 1}],
    },
    sort_keys=True,
).encode()
BODY_SHA = hashlib.sha256(BODY).hexdigest()
EVENT_TYPE = "canon.phase3.requested"
EVENT_ID = f"{CANON_KEY}:{EVENT_TYPE}"
API_URL = "http://127.0.0.1:25111"
MCP_URL = "http://127.0.0.1:25112/mcp"
DATABASE_URL = "postgresql://fixture:secret@127.0.0.1:25113/forwin"
MINIO_URL = "http://127.0.0.1:25115"


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def endpoint_identity() -> dict[str, Any]:
    digest = lambda label: hashlib.sha256(  # noqa: E731
        f"{FAULT_ID}:{label}".encode()
    ).hexdigest()
    run_id = digest("run")[:32]
    record: dict[str, Any] = {
        "schema_version": 1,
        "fault_id": FAULT_ID,
        "run_id": run_id,
        "source_sha": SOURCE_SHA,
        "source_tree": "1" * 40,
        "project_name": f"forwin-v5-recovery-{run_id}",
        "candidate_manifest_sha256": digest("manifest"),
        "candidate_identity_sha256": "",
        "sentinel": {
            "table": "forwin_recovery_run_sentinel",
            "sentinel_id": digest("sentinel"),
            "run_id": run_id,
            "fault_id": FAULT_ID,
            "source_sha": SOURCE_SHA,
        },
        "api": {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 25111,
            "endpoint_path": "",
            "health_path": "/health",
            "health_status": 200,
            "service": "forwin",
            "container_port": 8899,
            "container_id": f"{FAULT_ID}-api",
            "image_id": "sha256:" + digest("runtime-image"),
        },
        "mcp": {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 25112,
            "endpoint_path": "/mcp",
            "health_path": "/health",
            "health_status": 200,
            "service": "forwin-mcp",
            "container_port": 8896,
            "container_id": f"{FAULT_ID}-mcp",
            "image_id": "sha256:" + digest("runtime-image"),
        },
        "database": {
            "scheme": "postgresql",
            "host": "127.0.0.1",
            "port": 25113,
            "database": "forwin",
            "service": "postgres",
            "container_port": 5432,
            "container_id": f"{FAULT_ID}-postgres",
            "image_id": "sha256:" + digest("postgres-image"),
        },
        "minio": {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 25115,
            "endpoint_path": "",
            "health_path": "/minio/health/ready",
            "health_status": 200,
            "service": "minio",
            "container_port": 9000,
            "container_id": f"{FAULT_ID}-minio",
            "image_id": "sha256:" + digest("minio-image"),
        },
    }
    candidate_identity = {
        "source_sha": SOURCE_SHA,
        "source_tree": record["source_tree"],
        "runtime_image": {"image_id": record["api"]["image_id"]},
        "browser_image": {
            "image_id": "sha256:" + digest("publisher-browser-image")
        },
        "dependency_images": {
            "postgres": {"image_id": record["database"]["image_id"]},
            "qdrant": {},
            "minio": {"image_id": record["minio"]["image_id"]},
        },
        "candidate_manifest": {
            "sha256": record["candidate_manifest_sha256"]
        },
    }
    record["candidate_identity_sha256"] = stable_hash(candidate_identity)
    record["identity_sha256"] = stable_hash(record)
    return record


def load_local_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> Any:
    assert MODULE_PATH.is_file(), "Task5 runner has not been implemented"
    return load_local_module("task5_minio_recovery", MODULE_PATH)


@pytest.fixture(scope="module")
def evidence() -> Any:
    return load_local_module("task5_recovery_evidence", EVALUATOR_PATH)


@pytest.fixture(scope="module")
def finalizer() -> Any:
    return load_local_module("task5_finalize_recovery", FINALIZER_PATH)


def test_task5_runner_file_exists() -> None:
    assert MODULE_PATH.is_file()


def test_task5_and_task4_share_one_common_implementation(runner: Any) -> None:
    assert COMMON_PATH.is_file()
    task4 = load_local_module(
        "task5_task4_runner",
        Path(__file__).with_name("generation_projection_recovery.py"),
    )
    assert runner.common is task4.common
    assert runner.OneChapterLifecycle is task4.OneChapterLifecycle
    assert runner.RecoveryController is task4.RecoveryController
    assert runner.EvidenceWriter is task4.EvidenceWriter
    assert runner.atomic_write_json_new is task4.atomic_write_json_new


class FakeObjectResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class FakeMinio:
    def __init__(self, *, key: str, body: bytes = BODY) -> None:
        self.key = key
        self.body = body
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.response: FakeObjectResponse | None = None

    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str,
        recursive: bool,
    ) -> list[Any]:
        self.calls.append(("list", (bucket, prefix, recursive)))
        return [
            SimpleNamespace(
                object_name=self.key,
                etag="list-etag-must-not-win",
                size=999,
                last_modified="2030-01-01T00:00:00Z",
            )
        ]

    def stat_object(self, bucket: str, key: str) -> Any:
        self.calls.append(("head", (bucket, key)))
        return SimpleNamespace(
            object_name=key,
            etag='"etag-a"',
            size=len(self.body),
            content_type="application/json",
            last_modified="2030-01-01T00:00:00Z",
            metadata={"x-amz-meta-created-at": "discard"},
        )

    def get_object(self, bucket: str, key: str) -> FakeObjectResponse:
        self.calls.append(("get", (bucket, key)))
        self.response = FakeObjectResponse(self.body)
        return self.response


def test_minio_inventory_uses_list_head_and_independent_byte_hash(
    runner: Any,
) -> None:
    key = runner.expected_world_object_key(
        project_id=PROJECT_ID,
        canon_id=CANON_ID,
        prefix="artifacts",
        content_sha256=BODY_SHA,
    )
    client = FakeMinio(key=key)
    inventory = runner.MinioInventory(
        client=client,
        bucket="fixture-bucket",
        prefix="artifacts",
        endpoint_url=MINIO_URL,
    )

    observed = inventory.project_objects(PROJECT_ID)

    assert observed == [
        {
            "key": key,
            "etag": "etag-a",
            "size": len(BODY),
            "content_type": "application/json",
            "content_sha256": BODY_SHA,
        }
    ]
    assert list(observed[0]) == [
        "key",
        "etag",
        "size",
        "content_type",
        "content_sha256",
    ]
    assert not any("time" in field or field.endswith("_at") for field in observed[0])
    assert [name for name, _ in client.calls] == ["list", "head", "get"]
    assert client.response is not None
    assert client.response.closed is True
    assert client.response.released is True


def test_expected_world_key_matches_production_composition(runner: Any) -> None:
    from forwin.observability.llm_trace import post_canon_trace_artifact_key
    from forwin.storage.artifacts import MinioObjectStore

    store = object.__new__(MinioObjectStore)
    store.prefix = "release-prefix"
    artifact_key = post_canon_trace_artifact_key(
        canon_commit_id=CANON_ID,
        step_name="world",
    )
    production = store._key(
        f"projects/{PROJECT_ID}/keyed/{artifact_key.removesuffix('.json')}_{BODY_SHA}.json"
    )

    assert (
        runner.expected_world_object_key(
            project_id=PROJECT_ID,
            canon_id=CANON_ID,
            prefix="release-prefix",
            content_sha256=BODY_SHA,
        )
        == production
    )
    assert PROJECT_ID in production
    assert CANON_ID in production


def test_approval_request_replay_preserves_exact_identity(runner: Any) -> None:
    request = runner.ApprovalRequest.for_fixture(
        api_url="https://api.example",
        project_id=PROJECT_ID,
        chapter_number=1,
        fault_id=FAULT_ID,
        candidate_id=CANDIDATE_ID,
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []
    responses: list[Any] = [
        runner.ApprovalTransportFailure("MinIO unavailable"),
        {
            "ok": True,
            "status": "accepted",
            "project_id": PROJECT_ID,
            "chapter_number": 1,
        },
    ]

    def transport(
        method: str,
        url: str,
        *,
        query: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        calls.append((method, url, dict(json_body or {})))
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    api = runner.ApprovalAPI(transport=transport)
    with pytest.raises(runner.ApprovalTransportFailure):
        api.send(request)
    result = api.send(request)

    assert result["status"] == "accepted"
    assert calls[0] == calls[1]
    assert request.identity_sha256 == runner.stable_hash(
        {
            "method": calls[0][0],
            "url": calls[0][1],
            "body": calls[0][2],
            "candidate_id": CANDIDATE_ID,
        }
    )
    from forwin.api_schema.review import ChapterReviewApproveRequest

    assert ChapterReviewApproveRequest.model_validate(calls[0][2]).model_dump() == (
        calls[0][2]
    )


def test_approval_transport_does_not_swallow_keyboard_interrupt(
    runner: Any,
) -> None:
    request = runner.ApprovalRequest.for_fixture(
        api_url="https://api.example",
        project_id=PROJECT_ID,
        chapter_number=1,
        fault_id=FAULT_ID,
        candidate_id=CANDIDATE_ID,
    )

    def interrupting_transport(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        runner.ApprovalAPI(transport=interrupting_transport).send(request)


def composed_text(statement: Any) -> str:
    as_string = getattr(statement, "as_string", None)
    return as_string() if callable(as_string) else str(statement)


class BarrierCursor:
    def __init__(self, connection: "BarrierConnection") -> None:
        self.connection = connection
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> "BarrierCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self,
        statement: Any,
        params: tuple[Any, ...] | list[Any] | None = None,
    ) -> "BarrierCursor":
        text = composed_text(statement)
        values = tuple(params or ())
        self.connection.executions.append((text, values))
        if "current_user" in text:
            self.rows = [{"database_role": "forwin"}]
        elif "pg_locks" in text and "barrier lock residue" not in text:
            self.rows = copy.deepcopy(self.connection.lock_rows)
        elif "barrier object residue" in text:
            self.rows = [{"residue_count": self.connection.object_residue}]
        elif "barrier lock residue" in text:
            self.rows = [{"residue_count": self.connection.lock_residue}]
        elif "pg_advisory_unlock" in text:
            self.rows = [{"unlocked": True}]
        else:
            self.rows = []
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.rows[0]) if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.rows)


class BarrierConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.lock_rows: list[dict[str, Any]] = []
        self.object_residue = 0
        self.lock_residue = 0
        self.closed = False
        self.autocommit = False

    def cursor(self) -> BarrierCursor:
        return BarrierCursor(self)

    def close(self) -> None:
        self.closed = True


class ConnectionFactory:
    def __init__(self, *connections: BarrierConnection) -> None:
        self.connections = list(connections)

    def __call__(self, _url: str) -> BarrierConnection:
        return self.connections.pop(0)


def barrier_lock_rows(barrier: Any, *, canon_count: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "pid": 401,
            "granted": True,
            "application_name": barrier.holder_application_name,
            "usename": "forwin",
            "backend_type": "client backend",
            "query": "SELECT pg_advisory_lock($1)",
            "wait_event_type": None,
            "blocking_pids": [],
            "canon_count": canon_count,
        },
        {
            "pid": 509,
            "granted": False,
            "application_name": barrier.waiter_application_name,
            "usename": "forwin",
            "backend_type": "client backend",
            "query": "INSERT INTO decision_events (...)",
            "wait_event_type": "Lock",
            "blocking_pids": [401],
            "canon_count": canon_count,
        },
    ]


def test_review_barrier_binds_fixture_request_role_and_one_canon(
    runner: Any,
) -> None:
    admin = BarrierConnection()
    holder = BarrierConnection()
    barrier = runner.ReviewApprovedBarrier(
        fault_id=FAULT_ID,
        database_url="postgresql://fixture",
        connect=ConnectionFactory(admin, holder),
    )
    request = runner.ApprovalRequest.for_fixture(
        api_url="http://api.example",
        project_id=PROJECT_ID,
        chapter_number=1,
        fault_id=FAULT_ID,
        candidate_id=CANDIDATE_ID,
    )
    barrier.install(request=request)
    admin.lock_rows = barrier_lock_rows(barrier)

    observation = barrier.observe_blocked_waiter()

    assert observation.holder_count == 1
    assert observation.waiter_count == 1
    assert observation.canon_count == 1
    assert observation.waiter_application_name == barrier.waiter_application_name
    assert observation.database_role == "forwin"
    assert observation.backend_type == "client backend"
    statements = "\n".join(text for text, _ in admin.executions)
    assert "decision_events" in statements
    assert "review_approved" in statements
    assert "NEW.actor_type = 'api'" in statements
    assert PROJECT_ID not in statements
    scope_insert = next(
        params
        for text, params in admin.executions
        if text.lstrip().startswith("INSERT INTO")
    )
    assert PROJECT_ID in scope_insert
    assert request.reason in scope_insert

    barrier.release()
    barrier.cleanup()
    assert barrier.residue == {"objects": 0, "locks": 0}
    assert admin.closed is True
    assert holder.closed is True


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda rows, barrier: rows.__setitem__(
                1, {**rows[1], "application_name": "wrong-waiter"}
            ),
            "approval API waiter",
        ),
        (
            lambda rows, barrier: [
                row.__setitem__("canon_count", 2) for row in rows
            ],
            "exactly one committed Canon",
        ),
    ],
)
def test_review_barrier_fails_closed_on_wrong_waiter_or_canon_count(
    runner: Any,
    mutate: Callable[[list[dict[str, Any]], Any], Any],
    match: str,
) -> None:
    admin = BarrierConnection()
    holder = BarrierConnection()
    barrier = runner.ReviewApprovedBarrier(
        fault_id=FAULT_ID,
        database_url="postgresql://fixture",
        connect=ConnectionFactory(admin, holder),
    )
    request = runner.ApprovalRequest.for_fixture(
        api_url="http://api.example",
        project_id=PROJECT_ID,
        chapter_number=1,
        fault_id=FAULT_ID,
        candidate_id=CANDIDATE_ID,
    )
    barrier.install(request=request)
    rows = barrier_lock_rows(barrier)
    mutate(rows, barrier)
    admin.lock_rows = rows

    with pytest.raises(runner.SetupBlocked, match=match):
        barrier.observe_blocked_waiter()
    barrier.cleanup()


def phase3_row(
    *,
    status: str = "processed",
    event_id: str = EVENT_ID,
    payload: dict[str, Any] | None = None,
    attempts: int = 3,
    lease_epoch: int = 4,
) -> dict[str, Any]:
    return {
        "id": "outbox-row-a",
        "event_id": event_id,
        "aggregate_type": "project",
        "aggregate_id": PROJECT_ID,
        "event_type": EVENT_TYPE,
        "payload_json": payload
        or {
            "schema_version": 1,
            "canon_commit_id": CANON_ID,
            "canon_idempotency_key": CANON_KEY,
            "project_id": PROJECT_ID,
            "chapter_number": 1,
            "candidate_id": CANDIDATE_ID,
        },
        "status": status,
        "attempts": attempts,
        "available_at": None,
        "worker_id": "outbox-worker-a",
        "lease_epoch": lease_epoch,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "processed_at": "2026-07-26T12:00:00+00:00",
        "error_message": "",
    }


class ReplayDatabase:
    def __init__(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        rowcount: int = 1,
    ) -> None:
        self.before = before
        self.after = after
        self.rowcount = rowcount
        self.fetches = 0
        self.mutations: list[tuple[str, tuple[Any, ...]]] = []

    def fetch_all(
        self, statement: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        assert "task5 phase3 outbox" in statement
        self.fetches += 1
        row = self.before if self.fetches == 1 else self.after
        return [copy.deepcopy(row)]

    def execute_conditional(
        self, statement: str, params: tuple[Any, ...]
    ) -> int:
        self.mutations.append((statement, params))
        return self.rowcount


def fixture(runner: Any) -> Any:
    return runner.FixtureContext(
        fixture_id=f"fixture-{FAULT_ID}",
        fault_id=FAULT_ID,
        project_id=PROJECT_ID,
        chapter_number=1,
        chapter_id=CHAPTER_ID,
        task_id=TASK_ID,
        candidate_id=CANDIDATE_ID,
        canon_id=CANON_ID,
        canon_natural_key=CANON_KEY,
    )


class BoundaryDatabase:
    def __init__(self, phase3: dict[str, Any]) -> None:
        self.phase3 = phase3

    def fetch_all(
        self,
        statement: str,
        _params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        if "task5 canon" in statement:
            return [canon_record()]
        if "task5 accepted bundle" in statement:
            return [accepted_record()]
        if "task5 phase3 outbox" in statement:
            return [copy.deepcopy(self.phase3)]
        raise AssertionError(statement)

    def execute_conditional(
        self,
        _statement: str,
        _params: tuple[Any, ...],
    ) -> int:
        raise AssertionError("boundary proof is read-only")


def test_post_canon_boundary_requires_exact_fixture_event_identity(
    runner: Any,
) -> None:
    uncommitted = runner.FixtureContext(
        fixture_id=f"fixture-{FAULT_ID}",
        fault_id=FAULT_ID,
        project_id=PROJECT_ID,
        chapter_number=1,
        chapter_id=CHAPTER_ID,
        task_id=TASK_ID,
        candidate_id=CANDIDATE_ID,
    )
    valid = runner.SQLCollector(BoundaryDatabase(phase3_row(status="pending")))

    observed = valid.wait_post_canon_boundary(
        uncommitted,
        timeout_seconds=0.01,
        poll_seconds=0,
    )

    assert observed.canon_id == CANON_ID
    assert observed.canon_natural_key == CANON_KEY
    drifted = phase3_row(status="pending")
    drifted["aggregate_id"] = "other-project"
    invalid = runner.SQLCollector(BoundaryDatabase(drifted))
    with pytest.raises(runner.SetupBlocked, match="one Canon"):
        invalid.wait_post_canon_boundary(
            uncommitted,
            timeout_seconds=0.01,
            poll_seconds=0,
        )


def test_same_event_replay_is_one_exact_conditional_update(runner: Any) -> None:
    before = phase3_row()
    after = {
        **before,
        "status": "pending",
        "worker_id": "",
        "lease_expires_at": None,
        "heartbeat_at": None,
        "processed_at": None,
        "error_message": "",
    }
    database = ReplayDatabase(before, after)
    collector = runner.SQLCollector(database)

    artifact = collector.release_processed_phase3_event(fixture(runner))

    assert artifact["release"]["conditional_rowcount"] == 1
    assert artifact["baseline"]["attempts"] == 3
    assert artifact["baseline"]["lease_epoch"] == 4
    assert artifact["release"]["attempts"] == 3
    assert artifact["release"]["lease_epoch"] == 4
    assert artifact["release"]["before_row_sha256"]
    assert artifact["release"]["after_row_sha256"]
    assert "identity_unchanged" not in artifact
    assert len(database.mutations) == 1
    statement, params = database.mutations[0]
    assert statement.lstrip().startswith("UPDATE outbox_events")
    assert "INSERT" not in statement.upper()
    for predicate in (
        "id = %s",
        "event_id = %s",
        "status = 'processed'",
        "worker_id = %s",
        "lease_epoch = %s",
        "payload_json = %s",
        "aggregate_type = %s",
        "aggregate_id = %s",
        "canon_idempotency_key",
    ):
        assert predicate in statement
    assert "outbox-row-a" in params
    assert EVENT_ID in params
    assert CANON_KEY in params


@pytest.mark.parametrize(
    ("rowcount", "after_mutation", "match"),
    [
        (0, {}, "rowcount"),
        (2, {}, "rowcount"),
        (1, {"event_id": "drifted-event"}, "identity changed"),
        (
            1,
            {
                "payload_json": {
                    **phase3_row()["payload_json"],
                    "canon_idempotency_key": "drifted-key",
                }
            },
            "identity changed",
        ),
        (1, {"attempts": 4}, "claim counters changed"),
        (1, {"lease_epoch": 5}, "claim counters changed"),
    ],
)
def test_same_event_replay_rejects_rowcount_or_identity_drift(
    runner: Any,
    rowcount: int,
    after_mutation: dict[str, Any],
    match: str,
) -> None:
    before = phase3_row()
    after = {
        **before,
        "status": "pending",
        "worker_id": "",
        "processed_at": None,
        **after_mutation,
    }
    collector = runner.SQLCollector(
        ReplayDatabase(before, after, rowcount=rowcount)
    )

    with pytest.raises(runner.RunnerError, match=match):
        collector.release_processed_phase3_event(fixture(runner))


def test_phase3_final_observation_exposes_real_worker_claim_advancement(
    runner: Any,
) -> None:
    final = phase3_row(status="processed", attempts=4, lease_epoch=5)
    collector = runner.SQLCollector(BoundaryDatabase(final))

    observation = collector.phase3_replay_observation(fixture(runner))

    assert observation["status"] == "processed"
    assert observation["attempts"] == 4
    assert observation["lease_epoch"] == 5
    assert observation["event_id"] == EVENT_ID


class InterruptDatabase:
    def fetch_all(
        self,
        _statement: str,
        _params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        raise KeyboardInterrupt


def test_convergence_poll_does_not_swallow_keyboard_interrupt(
    runner: Any,
) -> None:
    collector = runner.SQLCollector(InterruptDatabase())

    with pytest.raises(KeyboardInterrupt):
        collector.wait_converged(
            fixture(runner),
            SimpleNamespace(),
            timeout_seconds=1,
            poll_seconds=0,
        )


def canon_record() -> dict[str, Any]:
    return {
        "canon_id": CANON_ID,
        "natural_key": CANON_KEY,
        "project_id": PROJECT_ID,
        "chapter_id": CHAPTER_ID,
        "chapter_number": 1,
        "candidate_id": CANDIDATE_ID,
        "canon_version": 1,
        "content_sha256": BODY_SHA,
    }


def accepted_record() -> dict[str, Any]:
    return {
        "bundle_id": CANON_ID,
        "candidate_id": CANDIDATE_ID,
        "project_id": PROJECT_ID,
        "chapter_id": CHAPTER_ID,
        "content_sha256": BODY_SHA,
    }


def trace_observation(*, status="pending", attempt=0, lease_epoch=0):
    payload = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "canon_commit_id": CANON_ID,
        "chapter_number": 1,
        "step_name": "world",
        "content": BODY.decode(),
        "content_sha256": BODY_SHA,
        "artifact_key": f"post_canon/{CANON_ID}/world/llm_trace_{BODY_SHA}.json",
    }
    return {
        "row_id": "trace-row-a",
        "event_id": f"maintenance-trace:{CANON_ID}:world:{BODY_SHA}",
        "aggregate_type": "project",
        "aggregate_id": PROJECT_ID,
        "event_type": "maintenance.trace.upload.requested",
        "payload": payload,
        "payload_sha256": stable_hash(payload),
        "status": status,
        "attempt": attempt,
        "lease_epoch": lease_epoch,
        "error_message": "",
    }


def valid_snapshots(
    runner: Any,
    kind: str,
    artifact: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    target = {
        "fixture_id": f"fixture-{FAULT_ID}",
        "fault_id": FAULT_ID,
        "resource_type": "chapter",
        "resource_id": CHAPTER_ID,
    }
    snapshots = {
        stage: runner.snapshot_envelope(
            source_sha=SOURCE_SHA,
            fault_kind=kind,
            fault_id=FAULT_ID,
            stage=stage,
            fixture=target,
            endpoint_identity=endpoint_identity(),
        )
        for stage in ("before", "during", "after")
    }
    if kind == "minio_pre_canon_unavailable":
        candidate = {
            "candidate_id": CANDIDATE_ID,
            "project_id": PROJECT_ID,
            "chapter_id": CHAPTER_ID,
            "content_sha256": BODY_SHA,
        }
        for snapshot in snapshots.values():
            snapshot["state"]["database"]["candidate"] = copy.deepcopy(candidate)
        snapshots["during"]["state"]["database"]["canon_commits"] = []
        snapshots["after"]["state"]["database"].update(
            canon_commits=[canon_record()],
            authoritative_identities=[
                {
                    "entity_type": "canon",
                    "record_id": CANON_ID,
                    "project_id": PROJECT_ID,
                    "chapter_id": CHAPTER_ID,
                    "natural_key": CANON_KEY,
                }
            ],
        )
        return snapshots
    for snapshot in snapshots.values():
        snapshot["state"]["database"].update(
            canon_commits=[canon_record()],
            accepted_bundles=[accepted_record()],
        )
    snapshots["during"]["state"]["database"]["maintenance"] = {
        "natural_key": f"post-canon-maintenance:v1:{CANON_KEY}:world",
        "project_id": PROJECT_ID,
        "canon_id": CANON_ID,
        "attempt": 1,
        "lease_epoch": 1,
        "status": "succeeded",
    }
    snapshots["after"]["state"]["database"].update(
        maintenance={
            "natural_key": f"post-canon-maintenance:v1:{CANON_KEY}:world",
            "project_id": PROJECT_ID,
            "canon_id": CANON_ID,
            "attempt": 1,
            "lease_epoch": 1,
            "status": "succeeded",
        },
        authoritative_identities=[
            {
                "entity_type": "canon",
                "record_id": CANON_ID,
                "project_id": PROJECT_ID,
                "chapter_id": CHAPTER_ID,
                "natural_key": CANON_KEY,
            }
        ],
        phase3_replay_baseline={
            **runner.SQLCollector._event_identity(phase3_row()),
            "status": "processed",
            "attempts": 3,
            "lease_epoch": 4,
        },
        phase3_replay_release={
            **runner.SQLCollector._event_identity(phase3_row(status="pending")),
            "status": "pending",
            "attempts": 3,
            "lease_epoch": 4,
            "conditional_rowcount": 1,
            "predicate_sha256": hashlib.sha256(b"predicate").hexdigest(),
            "before_row_sha256": hashlib.sha256(b"before").hexdigest(),
            "after_row_sha256": hashlib.sha256(b"after").hexdigest(),
        },
        phase3_replay_final={
            **runner.SQLCollector._event_identity(
                phase3_row(
                    status="processed",
                    attempts=4,
                    lease_epoch=5,
                )
            ),
            "status": "processed",
            "attempts": 4,
            "lease_epoch": 5,
        },
    )
    snapshots["during"]["state"]["database"]["trace_upload"] = trace_observation()
    snapshots["after"]["state"]["database"]["trace_upload"] = trace_observation(
        status="processed", attempt=1, lease_epoch=1
    )
    snapshots["after"]["state"]["external"][
        "replay_baseline_artifact"
    ] = copy.deepcopy(artifact)
    snapshots["after"]["state"]["external"]["artifact"] = copy.deepcopy(artifact)
    snapshots["after"]["state"]["barrier"]["residue_count"] = 0
    return snapshots


def append_event(
    finalizer: Any,
    events: list[dict[str, Any]],
    *,
    action: str,
    recorded_at: str,
    identity: dict[str, Any],
    run_identity: dict[str, Any],
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


def valid_event_log(
    finalizer: Any,
    evidence: Any,
    evidence_dir: Path,
    *,
    kind: str,
) -> Path:
    endpoint = endpoint_identity()
    run_identity = {
        "run_id": endpoint["run_id"],
        "evidence_directory": str(evidence_dir.resolve()),
        "database_volume_name": (
            f"forwin-v5-recovery-{endpoint['run_id']}-postgres-data"
        ),
    }
    created_at = "2026-07-26T12:00:00+00:00"
    volume = {
        "name": run_identity["database_volume_name"],
        "exists": True,
        "created_at": created_at,
        "fingerprint": evidence.stable_hash(
            {"created_at": created_at, "name": run_identity["database_volume_name"]}
        ),
    }
    absent = {"name": run_identity["database_volume_name"], "exists": False}
    identity = {
        "source_sha": SOURCE_SHA,
        "source_tree": endpoint["source_tree"],
        "runtime_image": {"image_id": endpoint["api"]["image_id"]},
        "browser_image": {
            "image_id": "sha256:"
            + hashlib.sha256(
                f"{FAULT_ID}:publisher-browser-image".encode()
            ).hexdigest()
        },
        "dependency_images": {
            "postgres": {"image_id": endpoint["database"]["image_id"]},
            "qdrant": {},
            "minio": {"image_id": endpoint["minio"]["image_id"]},
        },
        "candidate_manifest": {
            "sha256": endpoint["candidate_manifest_sha256"]
        },
    }
    worker_image = endpoint["api"]["image_id"]

    def worker_state(running: bool, *, probe: bool = False) -> dict[str, Any]:
        state = {
            "service": "outbox-worker",
            "exists": True,
            "running": running,
            "container_id": f"{FAULT_ID}-outbox-worker",
            "image_id": worker_image,
        }
        if probe:
            state["probe"] = {"passed": True}
        return state

    events: list[dict[str, Any]] = []
    append_event(
        finalizer,
        events,
        action="fresh_up_started",
        recorded_at="2026-07-26T11:59:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=absent,
        requested_at="2026-07-26T11:59:00+00:00",
    )
    append_event(
        finalizer,
        events,
        action="fresh_up_completed",
        recorded_at="2026-07-26T12:00:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=volume,
        requested_at="2026-07-26T11:59:00+00:00",
        sentinel=copy.deepcopy(endpoint["sentinel"]),
        after={
            "services": {
                **{
                    published["service"]: {
                        "exists": True,
                        "running": True,
                        "container_id": published["container_id"],
                        "image_id": published["image_id"],
                    }
                    for published in (
                        endpoint["api"],
                        endpoint["mcp"],
                        endpoint["database"],
                        endpoint["minio"],
                    )
                },
                "outbox-worker": worker_state(True),
            }
        },
    )
    append_event(
        finalizer,
        events,
        action="endpoints_bound",
        recorded_at="2026-07-26T12:00:01+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=volume,
        endpoint_identity=copy.deepcopy(endpoint),
    )
    if kind == "minio_post_canon_unavailable":
        append_event(
            finalizer,
            events,
            action="setup_service_held",
            recorded_at="2026-07-26T12:00:20+00:00",
            identity=identity,
            run_identity=run_identity,
            volume=volume,
            service="outbox-worker",
            hold_id="pre-approval",
            fault_kind=kind,
            purpose="auxiliary",
            requested_at="2026-07-26T12:00:10+00:00",
            hold_time="2026-07-26T12:00:20+00:00",
            before=worker_state(True),
            after=worker_state(False),
        )
    append_event(
        finalizer,
        events,
        action="fault_service_stopped",
        recorded_at="2026-07-26T12:01:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=volume,
        service="minio",
        requested_at="2026-07-26T12:00:50+00:00",
        fault_time="2026-07-26T12:01:00+00:00",
    )
    append_event(
        finalizer,
        events,
        action="fault_service_recovered",
        recorded_at="2026-07-26T12:02:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=volume,
        service="minio",
        requested_at="2026-07-26T12:01:50+00:00",
        recovery_time="2026-07-26T12:02:00+00:00",
    )
    if kind == "minio_post_canon_unavailable":
        append_event(
            finalizer,
            events,
            action="setup_service_released",
            recorded_at="2026-07-26T12:02:20+00:00",
            identity=identity,
            run_identity=run_identity,
            volume=volume,
            service="outbox-worker",
            hold_id="pre-approval",
            fault_kind=kind,
            purpose="auxiliary",
            requested_at="2026-07-26T12:02:10+00:00",
            release_time="2026-07-26T12:02:20+00:00",
            before=worker_state(False),
            after=worker_state(True, probe=True),
        )
        append_event(
            finalizer,
            events,
            action="setup_service_held",
            recorded_at="2026-07-26T12:02:40+00:00",
            identity=identity,
            run_identity=run_identity,
            volume=volume,
            service="outbox-worker",
            hold_id="same-event-replay",
            fault_kind=kind,
            purpose="auxiliary",
            requested_at="2026-07-26T12:02:30+00:00",
            hold_time="2026-07-26T12:02:40+00:00",
            before=worker_state(True),
            after=worker_state(False),
        )
        append_event(
            finalizer,
            events,
            action="setup_service_released",
            recorded_at="2026-07-26T12:03:00+00:00",
            identity=identity,
            run_identity=run_identity,
            volume=volume,
            service="outbox-worker",
            hold_id="same-event-replay",
            fault_kind=kind,
            purpose="auxiliary",
            requested_at="2026-07-26T12:02:50+00:00",
            release_time="2026-07-26T12:03:00+00:00",
            before=worker_state(False),
            after=worker_state(True, probe=True),
        )
    destroyed_services = {
        service: {"exists": False, "running": False}
        for service in finalizer.DESTROY_SERVICES
    }
    append_event(
        finalizer,
        events,
        action="destroyed",
        recorded_at="2026-07-26T12:04:00+00:00",
        identity=identity,
        run_identity=run_identity,
        volume=absent,
        requested_at="2026-07-26T12:03:50+00:00",
        database_volume_before=volume,
        after={"services": destroyed_services},
    )
    path = evidence_dir / "stack-events.jsonl"
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "kind",
    ("minio_pre_canon_unavailable", "minio_post_canon_unavailable"),
)
def test_task5_success_uses_real_writer_evaluator_and_finalizer(
    kind: str,
    runner: Any,
    evidence: Any,
    finalizer: Any,
    tmp_path: Path,
) -> None:
    evidence_dir = (tmp_path / kind).resolve()
    evidence_dir.mkdir()
    artifact = {
        "key": runner.expected_world_object_key(
            project_id=PROJECT_ID,
            canon_id=CANON_ID,
            prefix="artifacts",
            content_sha256=BODY_SHA,
        ),
        "etag": "etag-a",
        "size": len(BODY),
        "content_type": "application/json",
        "content_sha256": BODY_SHA,
    }
    snapshots = valid_snapshots(runner, kind, artifact)
    event_log = valid_event_log(
        finalizer,
        evidence,
        evidence_dir,
        kind=kind,
    )
    supplemental = {
        "request-replay.json": {
            "schema_version": 1,
            "request_identity_sha256": hashlib.sha256(b"request").hexdigest(),
        }
    }
    if kind == "minio_post_canon_unavailable":
        supplemental["same-event-replay.json"] = {
            "schema_version": 1,
            "baseline": copy.deepcopy(
                snapshots["after"]["state"]["database"][
                    "phase3_replay_baseline"
                ]
            ),
            "release": copy.deepcopy(
                snapshots["after"]["state"]["database"][
                    "phase3_replay_release"
                ]
            ),
        }
    writer = runner.EvidenceWriter(
        evidence_dir=evidence_dir,
        runner_path=MODULE_PATH,
        evaluator=evidence,
        report_validator=finalizer.fault_report_violations,
    )

    report_path = writer.write_pass_report(
        fault_kind=kind,
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        snapshots=snapshots,
        event_log_path=event_log,
        supplemental_artifacts=supplemental,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["result"] == "pass"
    assert finalizer.fault_report_violations(
        report,
        source_sha=SOURCE_SHA,
    ) == []
    assert evidence.assertion_violations(
        kind,
        report["assertions"],
    ) == []
    with pytest.raises(runner.RunnerError, match="already exists"):
        writer.write_pass_report(
            fault_kind=kind,
            fault_id=FAULT_ID,
            source_sha=SOURCE_SHA,
            snapshots=snapshots,
            event_log_path=event_log,
        )


class FakeLifecycle:
    mcp_url = MCP_URL

    async def create_genesis_project(self) -> Any:
        return SimpleNamespace(project_id=PROJECT_ID)

    async def start_writing(self, project_id: str) -> Any:
        assert project_id == PROJECT_ID
        return SimpleNamespace(project_id=project_id, task_id=TASK_ID)


class LiveController:
    def __init__(
        self,
        *,
        evidence_dir: Path,
        kind: str,
        evidence: Any,
        finalizer: Any,
        calls: list[str],
    ) -> None:
        self.evidence_dir = evidence_dir
        self.kind = kind
        self.evidence = evidence
        self.finalizer = finalizer
        self.calls = calls
        self.event_log_path = evidence_dir / "stack-events.jsonl"

    def fresh_up(self, fault_id: str) -> None:
        assert fault_id == FAULT_ID
        self.evidence_dir.mkdir()
        self.calls.append("fresh-up")

    def bind_endpoints(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["fault_id"] == FAULT_ID
        assert kwargs["minio_url"] == MINIO_URL
        self.calls.append("bind-endpoints")
        return endpoint_identity()

    def stop(self, service: str, fault_id: str) -> None:
        assert fault_id == FAULT_ID
        self.calls.append(f"stop:{service}")

    def start(self, service: str, fault_id: str) -> None:
        assert fault_id == FAULT_ID
        self.calls.append(f"start:{service}")

    def setup_hold(
        self,
        service: str,
        fault_id: str,
        hold_id: str,
        *,
        fault_kind: str,
        purpose: str = "auxiliary",
    ) -> None:
        assert fault_id == FAULT_ID
        assert fault_kind == self.kind
        assert purpose == "auxiliary"
        self.calls.append(f"hold:{service}:{hold_id}")

    def setup_release(
        self,
        service: str,
        fault_id: str,
        hold_id: str,
    ) -> None:
        assert fault_id == FAULT_ID
        self.calls.append(f"release:{service}:{hold_id}")

    def destroy(self) -> None:
        self.calls.append("destroy")
        valid_event_log(
            self.finalizer,
            self.evidence,
            self.evidence_dir,
            kind=self.kind,
        )

    def abort(self, fault_id: str, stage: str, reason: str) -> None:
        self.calls.append(f"abort:{fault_id}:{stage}:{reason}")

    def interrupt_cleanup(self, fault_id: str) -> None:
        self.calls.append(f"interrupt-cleanup:{fault_id}")


class FakeInventory:
    def __init__(self, calls: list[str], artifact: dict[str, Any]) -> None:
        self.calls = calls
        self.artifact = artifact
        self.prefix = "artifacts"
        self.endpoint_url = MINIO_URL

    def close(self) -> None:
        self.calls.append("inventory-close")


class SequencedApprovalAPI:
    def __init__(self, runner: Any, calls: list[str]) -> None:
        self.runner = runner
        self.calls = calls
        self.api_url = API_URL
        self.identities: list[str] = []

    def send(self, request: Any) -> dict[str, Any]:
        self.identities.append(request.identity_sha256)
        self.calls.append(f"approval:{len(self.identities)}")
        if len(self.identities) == 1:
            raise self.runner.ApprovalTransportFailure("MinIO unavailable")
        return {"ok": True, "status": "accepted"}


class PreCanonSQL:
    def __init__(
        self,
        runner: Any,
        calls: list[str],
        snapshots: dict[str, dict[str, Any]],
        api: SequencedApprovalAPI,
        artifact: dict[str, Any],
    ) -> None:
        self.runner = runner
        self.calls = calls
        self.snapshots = snapshots
        self.api = api
        self.artifact = artifact

    def read_recovery_sentinel(self) -> dict[str, Any]:
        return copy.deepcopy(endpoint_identity()["sentinel"])

    def bind_endpoint_identity(self, identity: dict[str, Any]) -> None:
        self.endpoint_identity = copy.deepcopy(identity)

    def wait_review_ready(self, **_kwargs: Any) -> Any:
        self.calls.append("review-ready")
        return fixture(self.runner)

    def pre_snapshot(self, *, stage: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"snapshot:{stage}")
        return copy.deepcopy(self.snapshots[stage])

    def require_pre_canon_failure(
        self,
        _fixture: Any,
        *,
        expected_candidate: Mapping[str, Any],
    ) -> None:
        assert expected_candidate["candidate_id"] == CANDIDATE_ID
        assert len(self.api.identities) == 1
        self.calls.append("canon-zero-candidate-stable")

    def _canon(self, _fixture: Any) -> list[dict[str, Any]]:
        return [canon_record()] if len(self.api.identities) == 2 else []

    def wait_converged(
        self,
        _fixture: Any,
        _inventory: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.calls.append("converged")
        return copy.deepcopy(self.artifact), [copy.deepcopy(self.artifact)]


def test_live_pre_canon_replays_the_identical_request_and_uses_real_pipeline(
    runner: Any,
    evidence: Any,
    finalizer: Any,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    evidence_dir = (tmp_path / "pre-live").resolve()
    artifact = {
        "key": runner.expected_world_object_key(
            project_id=PROJECT_ID,
            canon_id=CANON_ID,
            prefix="artifacts",
            content_sha256=BODY_SHA,
        ),
        "etag": "etag-a",
        "size": len(BODY),
        "content_type": "application/json",
        "content_sha256": BODY_SHA,
    }
    snapshots = valid_snapshots(
        runner,
        "minio_pre_canon_unavailable",
        artifact,
    )
    api = SequencedApprovalAPI(runner, calls)
    controller = LiveController(
        evidence_dir=evidence_dir,
        kind="minio_pre_canon_unavailable",
        evidence=evidence,
        finalizer=finalizer,
        calls=calls,
    )
    live = runner.LiveRunner(
        fault_kind="minio_pre_canon_unavailable",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        controller=controller,
        lifecycle=FakeLifecycle(),
        sql_collector=PreCanonSQL(
            runner,
            calls,
            snapshots,
            api,
            artifact,
        ),
        api=api,
        inventory=FakeInventory(calls, artifact),
        writer=runner.EvidenceWriter(
            evidence_dir=evidence_dir,
            runner_path=MODULE_PATH,
            evaluator=evidence,
            finalizer=finalizer,
        ),
        barrier_factory=lambda: pytest.fail("pre-Canon installed a barrier"),
        api_url=API_URL,
        mcp_url=MCP_URL,
        database_url=DATABASE_URL,
        minio_url=MINIO_URL,
    )

    result = live.run()

    assert result.status == "pass"
    assert api.identities == [api.identities[0], api.identities[0]]
    assert calls == [
        "fresh-up",
        "bind-endpoints",
        "review-ready",
        "snapshot:before",
        "stop:minio",
        "approval:1",
        "canon-zero-candidate-stable",
        "snapshot:during",
        "start:minio",
        "approval:2",
        "converged",
        "snapshot:after",
        "destroy",
        "inventory-close",
    ]
    assert json.loads(result.report_path.read_text(encoding="utf-8"))[
        "result"
    ] == "pass"


class BlockingApprovalAPI:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.api_url = API_URL
        self.release_event = threading.Event()

    def send(self, _request: Any) -> dict[str, Any]:
        self.calls.append("approval:blocked")
        if not self.release_event.wait(timeout=2):
            raise RuntimeError("barrier was not released")
        self.calls.append("approval:accepted")
        return {"ok": True, "status": "accepted"}


class FakeReviewBarrier:
    def __init__(self, runner: Any, api: BlockingApprovalAPI, calls: list[str]):
        self.runner = runner
        self.api = api
        self.calls = calls
        self.residue: dict[str, int] | None = None

    def install(self, *, request: Any) -> None:
        assert request.reason == f"recovery-evidence:{FAULT_ID}:{CANDIDATE_ID}"
        self.calls.append("barrier:install")

    def wait_for_blocked_waiter(self) -> Any:
        assert not self.api.release_event.is_set()
        self.calls.append("barrier:one-canon-bound-api-waiter")
        return self.runner.BarrierObservation(
            holder_pid=11,
            waiter_pid=12,
            holder_count=1,
            waiter_count=1,
            canon_count=1,
            waiter_application_name="task5-api",
            database_role="forwin",
            backend_type="client backend",
        )

    def release(self) -> None:
        if not self.api.release_event.is_set():
            self.calls.append("barrier:release")
            self.api.release_event.set()

    def cleanup(self) -> None:
        self.calls.append("barrier:cleanup-zero")
        self.residue = {"objects": 0, "locks": 0}


class PostCanonSQL:
    def __init__(
        self,
        runner: Any,
        calls: list[str],
        snapshots: dict[str, dict[str, Any]],
        artifact: dict[str, Any],
    ) -> None:
        self.runner = runner
        self.calls = calls
        self.snapshots = snapshots
        self.artifact = artifact
        self.convergence_count = 0

    def read_recovery_sentinel(self) -> dict[str, Any]:
        return copy.deepcopy(endpoint_identity()["sentinel"])

    def bind_endpoint_identity(self, identity: dict[str, Any]) -> None:
        self.endpoint_identity = copy.deepcopy(identity)

    def wait_review_ready(self, **_kwargs: Any) -> Any:
        self.calls.append("review-ready")
        return fixture(self.runner)

    def wait_post_canon_boundary(self, value: Any) -> Any:
        self.calls.append("boundary:one-canon-pending-phase3")
        return self.runner.FixtureContext(
            **{
                field: getattr(value, field)
                for field in (
                    "fixture_id",
                    "fault_id",
                    "project_id",
                    "chapter_number",
                    "chapter_id",
                    "task_id",
                    "candidate_id",
                )
            },
            canon_id=CANON_ID,
            canon_natural_key=CANON_KEY,
        )

    def post_snapshot(self, *, stage: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"snapshot:{stage}")
        if stage == "after":
            database = self.snapshots[stage]["state"]["database"]
            for field in (
                "phase3_replay_baseline",
                "phase3_replay_release",
                "phase3_replay_final",
            ):
                assert kwargs[field] == database[field]
        return copy.deepcopy(self.snapshots[stage])

    def require_maintenance_ready_with_trace_pending(self, _fixture: Any) -> None:
        self.calls.append("maintenance:succeeded-trace-pending")

    def wait_converged(
        self,
        _fixture: Any,
        _inventory: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.convergence_count += 1
        self.calls.append(f"converged:{self.convergence_count}")
        return copy.deepcopy(self.artifact), [copy.deepcopy(self.artifact)]

    def identity_world(
        self,
        _fixture: Any,
        objects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "canon": [canon_record()],
            "accepted_bundles": [accepted_record()],
            "phase3_event": {"event_id": EVENT_ID},
            "maintenance": [
                {"natural_key": f"{CANON_KEY}:{step}"}
                for step in self.runner.POST_CANON_STEPS
            ],
            "objects": copy.deepcopy(objects),
        }

    def release_processed_phase3_event(self, _fixture: Any) -> dict[str, Any]:
        self.calls.append("replay:conditional-rowcount-one")
        return {
            "schema_version": 1,
            "fault_id": FAULT_ID,
            "fixture": fixture(self.runner).evaluator_identity(),
            "baseline": copy.deepcopy(
                self.snapshots["after"]["state"]["database"][
                    "phase3_replay_baseline"
                ]
            ),
            "release": copy.deepcopy(
                self.snapshots["after"]["state"]["database"][
                    "phase3_replay_release"
                ]
            ),
        }

    def phase3_replay_observation(self, _fixture: Any) -> dict[str, Any]:
        return copy.deepcopy(
            self.snapshots["after"]["state"]["database"][
                "phase3_replay_final"
            ]
        )


def test_live_post_canon_uses_holds_barrier_replay_and_real_pipeline(
    runner: Any,
    evidence: Any,
    finalizer: Any,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    evidence_dir = (tmp_path / "post-live").resolve()
    artifact = {
        "key": runner.expected_world_object_key(
            project_id=PROJECT_ID,
            canon_id=CANON_ID,
            prefix="artifacts",
            content_sha256=BODY_SHA,
        ),
        "etag": "etag-a",
        "size": len(BODY),
        "content_type": "application/json",
        "content_sha256": BODY_SHA,
    }
    snapshots = valid_snapshots(
        runner,
        "minio_post_canon_unavailable",
        artifact,
    )
    api = BlockingApprovalAPI(calls)
    controller = LiveController(
        evidence_dir=evidence_dir,
        kind="minio_post_canon_unavailable",
        evidence=evidence,
        finalizer=finalizer,
        calls=calls,
    )
    sql_collector = PostCanonSQL(runner, calls, snapshots, artifact)
    live = runner.LiveRunner(
        fault_kind="minio_post_canon_unavailable",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        controller=controller,
        lifecycle=FakeLifecycle(),
        sql_collector=sql_collector,
        api=api,
        inventory=FakeInventory(calls, artifact),
        writer=runner.EvidenceWriter(
            evidence_dir=evidence_dir,
            runner_path=MODULE_PATH,
            evaluator=evidence,
            finalizer=finalizer,
        ),
        barrier_factory=lambda: FakeReviewBarrier(runner, api, calls),
        api_url=API_URL,
        mcp_url=MCP_URL,
        database_url=DATABASE_URL,
        minio_url=MINIO_URL,
    )

    result = live.run()

    assert result.status == "pass"
    assert calls == [
        "fresh-up",
        "bind-endpoints",
        "review-ready",
        "hold:outbox-worker:pre-approval",
        "barrier:install",
        "approval:blocked",
        "barrier:one-canon-bound-api-waiter",
        "boundary:one-canon-pending-phase3",
        "snapshot:before",
        "stop:minio",
        "barrier:release",
        "approval:accepted",
        "maintenance:succeeded-trace-pending",
        "snapshot:during",
        "barrier:cleanup-zero",
        "start:minio",
        "release:outbox-worker:pre-approval",
        "converged:1",
        "hold:outbox-worker:same-event-replay",
        "replay:conditional-rowcount-one",
        "release:outbox-worker:same-event-replay",
        "converged:2",
        "snapshot:after",
        "destroy",
        "inventory-close",
    ]
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["result"] == "pass"
    assert {item["name"] for item in report["supplemental_artifacts"]} == {
        "barrier-observation.json",
        "minio-inventory.json",
        "same-event-replay.json",
    }


class UnexpectedApprovalAPI:
    api_url = API_URL

    def send(self, _request: Any) -> dict[str, Any]:
        return {"ok": True, "status": "accepted"}


def test_unobserved_fault_aborts_and_can_only_report_setup_blocked(
    runner: Any,
    evidence: Any,
    finalizer: Any,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    evidence_dir = (tmp_path / "setup-blocked").resolve()
    artifact = {
        "key": runner.expected_world_object_key(
            project_id=PROJECT_ID,
            canon_id=CANON_ID,
            prefix="artifacts",
            content_sha256=BODY_SHA,
        ),
        "etag": "etag-a",
        "size": len(BODY),
        "content_type": "application/json",
        "content_sha256": BODY_SHA,
    }
    controller = LiveController(
        evidence_dir=evidence_dir,
        kind="minio_pre_canon_unavailable",
        evidence=evidence,
        finalizer=finalizer,
        calls=calls,
    )
    api = UnexpectedApprovalAPI()
    sql_collector = PreCanonSQL(
        runner,
        calls,
        valid_snapshots(
            runner,
            "minio_pre_canon_unavailable",
            artifact,
        ),
        api,
        artifact,
    )
    live = runner.LiveRunner(
        fault_kind="minio_pre_canon_unavailable",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        controller=controller,
        lifecycle=FakeLifecycle(),
        sql_collector=sql_collector,
        api=api,
        inventory=FakeInventory(calls, artifact),
        writer=runner.EvidenceWriter(
            evidence_dir=evidence_dir,
            runner_path=MODULE_PATH,
            evaluator=evidence,
            finalizer=finalizer,
        ),
        barrier_factory=lambda: pytest.fail("pre-Canon installed a barrier"),
        api_url=API_URL,
        mcp_url=MCP_URL,
        database_url=DATABASE_URL,
        minio_url=MINIO_URL,
    )

    result = live.run()

    assert result.status == "setup_blocked"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["result"] == "setup_blocked"
    assert "did not fail" in report["failure_reason"]
    assert "destroy" not in calls
    assert any(call.startswith(f"abort:{FAULT_ID}:approval_failure:") for call in calls)
    assert "inventory-close" in calls
    assert calls.index("inventory-close") < next(
        index for index, call in enumerate(calls) if call.startswith("abort:")
    )


def test_failure_cleanup_closes_barrier_before_joining_async_request(
    runner: Any,
) -> None:
    state = {"cleaned": False, "joined": False}

    class FailingReleaseBarrier:
        def release(self) -> None:
            raise RuntimeError("unlock failed")

        def cleanup(self) -> None:
            state["cleaned"] = True

    class CleanupBoundApproval:
        def join(self, *, timeout_seconds: float) -> dict[str, Any]:
            assert timeout_seconds == 120.0
            assert state["cleaned"] is True
            state["joined"] = True
            return {"ok": True, "status": "accepted"}

    live = object.__new__(runner.LiveRunner)
    live.barrier = FailingReleaseBarrier()
    live.async_approval = CleanupBoundApproval()

    errors = live._cleanup_local_resources()

    assert errors == ["barrier release: unlock failed"]
    assert state == {"cleaned": True, "joined": True}
    assert live.barrier is None
    assert live.async_approval is None


@pytest.mark.parametrize("interruption", (KeyboardInterrupt, SystemExit))
def test_task5_caller_owns_stack_before_fresh_up_returns(
    runner: Any,
    interruption: type[BaseException],
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class InterruptingController:
        event_log_path = tmp_path / "events.jsonl"

        def fresh_up(self, _fault_id: str) -> None:
            calls.append("fresh-up")
            raise interruption()

        def interrupt_cleanup(self, fault_id: str) -> None:
            assert fault_id == FAULT_ID
            calls.append("interrupt-cleanup")

    class ClosingInventory:
        def close(self) -> None:
            calls.append("inventory-close")

    class NoReportWriter:
        def write_setup_blocked(self, **_kwargs: Any) -> Path:
            pytest.fail("interruption emitted setup_blocked")

        def write_pass_report(self, **_kwargs: Any) -> Path:
            pytest.fail("interruption emitted PASS")

    live = runner.LiveRunner(
        fault_kind="minio_pre_canon_unavailable",
        fault_id=FAULT_ID,
        source_sha=SOURCE_SHA,
        controller=InterruptingController(),
        lifecycle=SimpleNamespace(),
        sql_collector=SimpleNamespace(),
        api=SimpleNamespace(),
        inventory=ClosingInventory(),
        writer=NoReportWriter(),
        barrier_factory=lambda: pytest.fail(
            "fresh-up interruption installed a barrier"
        ),
        api_url=API_URL,
        mcp_url=MCP_URL,
        database_url=DATABASE_URL,
        minio_url=MINIO_URL,
    )

    with pytest.raises(interruption):
        live.run()

    assert calls == ["fresh-up", "inventory-close", "interrupt-cleanup"]


def test_controller_client_uses_hold_release_abort_and_never_docker(
    runner: Any,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def execute(args: list[str], **_kwargs: Any) -> Any:
        calls.append(list(args))
        is_abort = "abort" in args
        return SimpleNamespace(
            returncode=2 if is_abort else 0,
            stdout="{}\n" if not is_abort else "",
            stderr=(
                "error: recovery abort: setup_failure=stage: reason"
                if is_abort
                else ""
            ),
        )

    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}", encoding="utf-8")
    controller = runner.RecoveryController(
        candidate_manifest=candidate,
        evidence_dir=tmp_path / "evidence",
        execute=execute,
        python_executable="/python",
    )

    controller.setup_hold(
        "outbox-worker",
        FAULT_ID,
        "pre-approval",
        fault_kind="minio_post_canon_unavailable",
    )
    controller.setup_release("outbox-worker", FAULT_ID, "pre-approval")
    controller.stop("minio", FAULT_ID)
    controller.start("minio", FAULT_ID)
    controller.abort(FAULT_ID, "stage", "reason")

    suffixes = [call[2:] for call in calls]
    assert suffixes == [
        [
            "setup-hold",
            "outbox-worker",
            "--fault-id",
            FAULT_ID,
            "--hold-id",
            "pre-approval",
            "--fault-kind",
            "minio_post_canon_unavailable",
            "--purpose",
            "auxiliary",
        ],
        [
            "setup-release",
            "outbox-worker",
            "--fault-id",
            FAULT_ID,
            "--hold-id",
            "pre-approval",
        ],
        ["stop", "minio", "--fault-id", FAULT_ID],
        ["start", "minio", "--fault-id", FAULT_ID],
        [
            "abort",
            "--fault-id",
            FAULT_ID,
            "--stage",
            "stage",
            "--reason",
            "reason",
        ],
    ]
    assert all("docker" not in call for call in calls)


def test_cli_accepts_only_task5_faults_and_parameterizes_endpoints(
    runner: Any,
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps({"source": {"sha": SOURCE_SHA}}),
        encoding="utf-8",
    )
    arguments = [
        "run",
        "--fault-kind",
        "minio_post_canon_unavailable",
        "--fault-id",
        FAULT_ID,
        "--candidate-manifest",
        str(candidate),
        "--mcp-url",
        MCP_URL,
        "--api-url",
        API_URL,
        "--database-url-env",
        "FIXTURE_DATABASE_URL",
        "--evidence-dir",
        str(tmp_path / "evidence"),
    ]
    parsed = runner.parse_args(arguments)
    config = runner.resolve_run_config(
        parsed,
        environ={
            "FIXTURE_DATABASE_URL": DATABASE_URL,
            "FORWIN_RECOVERY_MINIO_ENDPOINT": "127.0.0.1:25115",
            "FORWIN_RECOVERY_MINIO_ACCESS_KEY": "access",
            "FORWIN_RECOVERY_MINIO_SECRET_KEY": "secret",
            "FORWIN_RECOVERY_MINIO_BUCKET": "bucket",
            "FORWIN_RECOVERY_MINIO_PREFIX": "prefix",
            "FORWIN_RECOVERY_MINIO_SECURE": "false",
        },
    )

    assert set(runner.SUPPORTED_FAULTS) == {
        "minio_pre_canon_unavailable",
        "minio_post_canon_unavailable",
    }
    assert config.mcp_url == MCP_URL
    assert config.api_url == API_URL
    assert config.database_url == DATABASE_URL
    assert config.minio_endpoint == "127.0.0.1:25115"
    assert config.minio_secure is False
    with pytest.raises(SystemExit):
        runner.parse_args(
            [
                *arguments[:2],
                "qdrant_unavailable",
                *arguments[3:],
            ]
        )


def test_read_only_production_schema_contrast() -> None:
    from forwin.models.audit import DecisionEvent
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.draft import CandidateDraftRecord
    from forwin.models.maintenance import PostCanonMaintenanceRun
    from forwin.models.outbox import OutboxEvent

    assert {
        "project_id",
        "chapter_number",
        "event_type",
        "actor_type",
        "reason",
    } <= set(DecisionEvent.__table__.columns.keys())
    assert {
        "id",
        "idempotency_key",
        "candidate_id",
        "project_id",
        "chapter_number",
        "status",
    } <= set(CanonCommitRecord.__table__.columns.keys())
    assert {
        "id",
        "project_id",
        "chapter_plan_id",
        "chapter_number",
        "body_hash",
        "status",
        "idempotency_key",
    } <= set(CandidateDraftRecord.__table__.columns.keys())
    assert {
        "id",
        "event_id",
        "aggregate_type",
        "aggregate_id",
        "event_type",
        "payload_json",
        "status",
        "attempts",
        "worker_id",
        "lease_epoch",
    } <= set(OutboxEvent.__table__.columns.keys())
    assert {
        "canon_commit_id",
        "project_id",
        "candidate_id",
        "step_name",
        "idempotency_key",
        "status",
        "attempts",
        "lease_epoch",
        "result_json",
    } <= set(PostCanonMaintenanceRun.__table__.columns.keys())
    routes = Path("forwin/http/routes.py").read_text(encoding="utf-8")
    assert (
        '"/api/projects/{project_id}/chapters/{chapter_number}/review/approve"'
        in routes
    )


def test_collector_requires_committed_steps_and_the_exact_pending_trace(runner):
    observation = trace_observation()
    trace_row = {
        **observation,
        "id": observation["row_id"],
        "payload_json": observation["payload"],
        "attempts": 0,
    }
    reference = {
        "event_id": observation["event_id"],
        "artifact_key": observation["payload"]["artifact_key"],
        "hash": BODY_SHA,
    }
    rows = [
        {
            "step_name": step,
            "status": "succeeded",
            "result_json": json.dumps({"trace": reference} if step == "world" else {}),
        }
        for step in runner.POST_CANON_STEPS
    ]

    class Database:
        def fetch_all(self, statement, params):
            assert params[0] == PROJECT_ID
            if "task5 maintenance" in statement:
                return rows
            if "task5 trace outbox" in statement:
                return [trace_row]
            raise AssertionError(statement)

    collector = runner.SQLCollector(Database())
    collector.require_maintenance_ready_with_trace_pending(fixture(runner))
    rows[0]["status"] = "failed"
    with pytest.raises(runner.SetupBlocked, match="completed maintenance"):
        collector.require_maintenance_ready_with_trace_pending(fixture(runner))
    rows[0]["status"] = "succeeded"
    trace_row["event_id"] = "unrelated-trace"
    with pytest.raises(runner.SetupBlocked, match="not bound"):
        collector.require_maintenance_ready_with_trace_pending(fixture(runner))
