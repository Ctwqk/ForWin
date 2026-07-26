#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from psycopg import sql


ARTIFACT_DIR = Path(__file__).resolve().parent
if str(ARTIFACT_DIR) not in sys.path:
    sys.path.insert(0, str(ARTIFACT_DIR))
import recovery_runner_common as common
from recovery_runner_common import (
    CONTROLLER_PATH,
    EVALUATOR_PATH,
    EVENT_LOG_NAME,
    FINALIZER_PATH,
    GENESIS_STAGES,
    REPORT_NAME,
    ROOT,
    EvidenceWriter,
    OneChapterLifecycle,
    ProjectFixture,
    RecoveryController,
    RunnerError,
    SetupBlocked,
    TaskFixture,
    atomic_write_json_new,
    canonical_digest,
    canonical_json,
    candidate_identity,
    http_json,
    normalize_database_url,
    psycopg_connect,
    required_text,
    required_url,
    sha256_file,
    validate_fault_id,
)

SUPPORTED_FAULTS = (
    "generation_worker_precommit_crash",
    "generation_worker_postcommit_crash",
    "qdrant_unavailable",
    "projection_consumer_unavailable",
)
GENERATION_FAULTS = frozenset(
    {
        "generation_worker_precommit_crash",
        "generation_worker_postcommit_crash",
    }
)
PROJECTION_FAULTS = frozenset(
    {"qdrant_unavailable", "projection_consumer_unavailable"}
)
SERVICE_BY_FAULT = {
    "generation_worker_precommit_crash": "generation-worker",
    "generation_worker_postcommit_crash": "generation-worker",
    "qdrant_unavailable": "qdrant",
    "projection_consumer_unavailable": "outbox-worker",
}
FAULT_EVENT_BY_KIND = {
    "generation_worker_precommit_crash": (
        "fault_service_killed",
        "crash_time",
    ),
    "generation_worker_postcommit_crash": (
        "fault_service_killed",
        "crash_time",
    ),
    "qdrant_unavailable": ("fault_service_stopped", "fault_time"),
    "projection_consumer_unavailable": (
        "fault_service_stopped",
        "fault_time",
    ),
}
RECOVERY_EVENT_ACTION = "fault_service_recovered"
GENERATION_WORKER_APPLICATION_NAME = "forwin-recovery-generation-worker"
GENERATION_WORKER_ROLE = "generation-worker"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
SQL_IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")
CANON_PROJECTION_PAYLOAD_KEYS = {
    "schema_version",
    "canon_commit_id",
    "canon_idempotency_key",
    "project_id",
    "chapter_number",
    "candidate_id",
    "trigger",
}


@dataclass(frozen=True, slots=True)
class BarrierNames:
    scope_table: str
    function: str
    trigger: str


def barrier_names(fault_id: str) -> BarrierNames:
    validate_fault_id(fault_id)
    digest = hashlib.sha256(fault_id.encode("ascii")).hexdigest()[:16]
    prefix = f"fw_recovery_{digest}"
    names = BarrierNames(
        scope_table=f"{prefix}_scope",
        function=f"{prefix}_fn",
        trigger=f"{prefix}_trg",
    )
    if any(
        SQL_IDENTIFIER_PATTERN.fullmatch(name) is None
        for name in (names.scope_table, names.function, names.trigger)
    ):
        raise RunnerError("generated barrier SQL identifier is unsafe")
    return names


def advisory_key(fault_id: str) -> int:
    validate_fault_id(fault_id)
    unsigned = int.from_bytes(
        hashlib.sha256(f"advisory:{fault_id}".encode("ascii")).digest()[:8],
        byteorder="big",
        signed=False,
    )
    value = unsigned if unsigned < 2**63 else unsigned - 2**64
    return value or 1


def advisory_lock_identity(key: int) -> tuple[int, int]:
    unsigned = key & ((1 << 64) - 1)
    return ((unsigned >> 32) & 0xFFFFFFFF, unsigned & 0xFFFFFFFF)


@dataclass(frozen=True, slots=True)
class BarrierObservation:
    holder_pid: int
    waiter_pid: int
    waiter_application_name: str
    target_role: str


class AdvisoryBarrier:
    def __init__(
        self,
        *,
        kind: str,
        fault_id: str,
        database_url: str,
        connect: Callable[[str], Any] = psycopg_connect,
    ) -> None:
        if kind not in GENERATION_FAULTS:
            raise RunnerError(f"unsupported generation barrier kind: {kind}")
        self.kind = kind
        self.fault_id = validate_fault_id(fault_id)
        self.database_url = normalize_database_url(database_url)
        self.connect = connect
        self.names = barrier_names(fault_id)
        self.advisory_key = advisory_key(fault_id)
        self.lock_identity = advisory_lock_identity(self.advisory_key)
        self.target_table = (
            "canon_commit_records"
            if kind == "generation_worker_precommit_crash"
            else "post_canon_maintenance_runs"
        )
        self.holder_application_name = (
            f"{self.names.scope_table[:48]}_holder"
        )
        self._admin: Any | None = None
        self._holder: Any | None = None
        self._installed = False
        self.last_residue_count: int | None = None

    def install(self, *, project_id: str, chapter_number: int) -> None:
        if not str(project_id or "").strip():
            raise RunnerError("barrier project_id is empty")
        if int(chapter_number or 0) < 1:
            raise RunnerError("barrier chapter_number must be positive")
        if self._admin is not None or self._holder is not None:
            raise RunnerError("barrier is already installed")
        self._installed = True
        try:
            self._admin = self.connect(self.database_url)
            self._admin.autocommit = True
            self._holder = self.connect(self.database_url)
            self._holder.autocommit = True
            if self._residue_count() != 0:
                raise SetupBlocked("fault-scoped barrier objects already exist")
            with self._admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "CREATE UNLOGGED TABLE {} ("
                        "project_id text NOT NULL, "
                        "chapter_number integer NOT NULL, "
                        "advisory_key bigint NOT NULL, "
                        "PRIMARY KEY (project_id, chapter_number)"
                        ")"
                    ).format(sql.Identifier(self.names.scope_table))
                )
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {} "
                        "(project_id, chapter_number, advisory_key) "
                        "VALUES (%s, %s, %s)"
                    ).format(sql.Identifier(self.names.scope_table)),
                    (str(project_id), int(chapter_number), self.advisory_key),
                )
                cursor.execute(self._function_statement())
                cursor.execute(
                    sql.SQL(
                        "CREATE TRIGGER {} BEFORE INSERT ON {} "
                        "FOR EACH ROW EXECUTE FUNCTION {}()"
                    ).format(
                        sql.Identifier(self.names.trigger),
                        sql.Identifier(self.target_table),
                        sql.Identifier(self.names.function),
                    )
                )
            with self._holder.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('application_name', %s, false)",
                    (self.holder_application_name,),
                )
                cursor.execute(
                    "SELECT pg_advisory_lock(%s)",
                    (self.advisory_key,),
                )
        except BaseException:
            try:
                self.cleanup()
            except BaseException:
                pass
            raise

    def _function_statement(self) -> sql.Composed:
        planning_guard = (
            sql.SQL(" AND NEW.step_name = 'planning'")
            if self.kind == "generation_worker_postcommit_crash"
            else sql.SQL("")
        )
        return sql.SQL(
            "CREATE FUNCTION {}() RETURNS trigger "
            "LANGUAGE plpgsql AS $forwin_recovery$ "
            "DECLARE scoped_key bigint; "
            "BEGIN "
            "SELECT advisory_key INTO scoped_key FROM {} "
            "WHERE project_id = NEW.project_id "
            "AND chapter_number = NEW.chapter_number{}; "
            "IF scoped_key IS NOT NULL THEN "
            "PERFORM pg_advisory_xact_lock(scoped_key); "
            "END IF; "
            "RETURN NEW; "
            "END "
            "$forwin_recovery$"
        ).format(
            sql.Identifier(self.names.function),
            sql.Identifier(self.names.scope_table),
            planning_guard,
        )

    def _residue_count(self) -> int:
        if self._admin is None:
            return 0
        statement = """
            SELECT (
                (SELECT count(*) FROM pg_trigger
                 WHERE tgname = %s AND NOT tgisinternal)
              + (SELECT count(*) FROM pg_proc WHERE proname = %s)
              + (SELECT count(*) FROM pg_class WHERE relname = %s)
            )::integer AS residue_count /* barrier residue */
        """
        with self._admin.cursor() as cursor:
            cursor.execute(
                statement,
                (
                    self.names.trigger,
                    self.names.function,
                    self.names.scope_table,
                ),
            )
            row = cursor.fetchone()
        return int((row or {}).get("residue_count", 0))

    def observe_blocked_waiter(self) -> BarrierObservation:
        if self._admin is None or self._holder is None:
            raise RunnerError("barrier is not installed")
        statement = """
            SELECT
                locks.pid,
                locks.granted,
                activity.application_name,
                activity.query,
                activity.wait_event_type,
                activity.wait_event,
                pg_blocking_pids(locks.pid) AS blocking_pids
            FROM pg_locks AS locks
            JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
            WHERE locks.locktype = 'advisory'
              AND locks.classid::bigint = %s
              AND locks.objid::bigint = %s
            ORDER BY locks.granted DESC, locks.pid
        """
        with self._admin.cursor() as cursor:
            cursor.execute(statement, self.lock_identity)
            rows = list(cursor.fetchall())
        holders = [
            row
            for row in rows
            if row.get("granted") is True
            and row.get("application_name") == self.holder_application_name
        ]
        waiters = [
            row
            for row in rows
            if row.get("granted") is False
            and row.get("wait_event_type") == "Lock"
            and self.target_table in str(row.get("query") or "")
        ]
        if len(rows) != 2 or len(holders) != 1 or len(waiters) != 1:
            raise SetupBlocked(
                "barrier requires exactly one holder and one waiter"
            )
        if (
            waiters[0].get("application_name")
            != GENERATION_WORKER_APPLICATION_NAME
        ):
            raise SetupBlocked(
                "barrier waiter does not have the exact generation-worker "
                "application_name"
            )
        holder_pid = int(holders[0]["pid"])
        waiter_pid = int(waiters[0]["pid"])
        blocking_pids = [int(value) for value in waiters[0].get("blocking_pids") or []]
        if blocking_pids != [holder_pid]:
            raise SetupBlocked(
                "barrier waiter is not blocked only by the scoped holder"
            )
        return BarrierObservation(
            holder_pid=holder_pid,
            waiter_pid=waiter_pid,
            waiter_application_name=GENERATION_WORKER_APPLICATION_NAME,
            target_role=GENERATION_WORKER_ROLE,
        )

    def wait_for_blocked_waiter(
        self,
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.5,
    ) -> BarrierObservation:
        deadline = time.monotonic() + timeout_seconds
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                return self.observe_blocked_waiter()
            except SetupBlocked as exc:
                last_error = exc
                time.sleep(poll_seconds)
        raise SetupBlocked(
            "generation barrier did not produce exactly one blocked waiter: "
            f"{last_error or 'timeout'}"
        )

    def cleanup(self) -> None:
        errors: list[str] = []
        if self._holder is not None:
            try:
                with self._holder.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(%s) AS unlocked",
                        (self.advisory_key,),
                    )
                    row = cursor.fetchone()
                    if row is not None and row.get("unlocked") is False:
                        errors.append("scoped advisory lock was not held")
            except BaseException as exc:
                errors.append(f"advisory unlock: {exc}")
            finally:
                try:
                    self._holder.close()
                except BaseException as exc:
                    errors.append(f"holder close: {exc}")
                self._holder = None
        if self._admin is not None:
            try:
                with self._admin.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                            sql.Identifier(self.names.trigger),
                            sql.Identifier(self.target_table),
                        )
                    )
                    cursor.execute(
                        sql.SQL("DROP FUNCTION IF EXISTS {}()").format(
                            sql.Identifier(self.names.function)
                        )
                    )
                    cursor.execute(
                        sql.SQL("DROP TABLE IF EXISTS {}").format(
                            sql.Identifier(self.names.scope_table)
                        )
                    )
                residue = self._residue_count()
                self.last_residue_count = residue
                if residue:
                    errors.append(f"barrier residue count is {residue}")
            except BaseException as exc:
                errors.append(f"barrier object cleanup: {exc}")
            finally:
                try:
                    self._admin.close()
                except BaseException as exc:
                    errors.append(f"admin close: {exc}")
                self._admin = None
        self._installed = False
        if errors:
            raise RunnerError("; ".join(errors))

    def __enter__(self) -> "AdvisoryBarrier":
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: Any,
    ) -> None:
        self.cleanup()

_required_text = required_text
_canonical_digest = canonical_digest


def normalize_canon_records(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        normalized.append(
            {
                "canon_id": _required_text(row.get("canon_id"), "canon_id"),
                "natural_key": _required_text(
                    row.get("natural_key"), "canon natural_key"
                ),
                "project_id": _required_text(row.get("project_id"), "project_id"),
                "chapter_id": _required_text(row.get("chapter_id"), "chapter_id"),
                "chapter_number": int(row.get("chapter_number") or 0),
                "candidate_id": _required_text(
                    row.get("candidate_id"),
                    "candidate_id",
                ),
                "canon_version": int(row.get("canon_version") or 0),
                "content_sha256": _canonical_digest(
                    row.get("content_sha256"),
                    "canon content_sha256",
                ),
            }
        )
    return normalized


def normalize_accepted_bundles(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "bundle_id": _required_text(row.get("bundle_id"), "bundle_id"),
            "candidate_id": _required_text(
                row.get("candidate_id"), "candidate_id"
            ),
            "project_id": _required_text(row.get("project_id"), "project_id"),
            "chapter_id": _required_text(row.get("chapter_id"), "chapter_id"),
            "content_sha256": _canonical_digest(
                row.get("content_sha256"),
                "bundle content_sha256",
            ),
        }
        for row in rows
    ]


def normalize_task(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": _required_text(row.get("task_id"), "task_id"),
        "lease_epoch": int(row.get("lease_epoch") or 0),
    }


def normalize_outbox_record(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_payload = row.get("payload_json")
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise SetupBlocked("projection outbox payload is invalid JSON") from exc
    elif isinstance(raw_payload, Mapping):
        payload = dict(raw_payload)
    else:
        raise SetupBlocked("projection outbox payload is missing")
    if not isinstance(payload, dict):
        raise SetupBlocked("projection outbox payload is not an object")
    if set(payload) != CANON_PROJECTION_PAYLOAD_KEYS:
        raise SetupBlocked("projection outbox payload schema is not exact")
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or type(payload.get("chapter_number")) is not int
        or payload["chapter_number"] < 1
    ):
        raise SetupBlocked("projection outbox payload version/chapter is invalid")
    for key in (
        "canon_commit_id",
        "canon_idempotency_key",
        "project_id",
        "candidate_id",
        "trigger",
    ):
        _required_text(payload.get(key), f"outbox payload {key}")
    status = _required_text(row.get("status"), "outbox status")
    if status not in {"pending", "running", "processed"}:
        raise SetupBlocked("projection outbox status is invalid")
    error_message = str(row.get("error_message") or "")
    if (
        "\x00" in error_message
        or len(error_message) > 4000
        or error_message != " ".join(error_message.split())
    ):
        raise SetupBlocked("projection outbox error is not sanitized")
    return {
        "event_id": _required_text(row.get("event_id"), "outbox event_id"),
        "aggregate_type": _required_text(
            row.get("aggregate_type"),
            "outbox aggregate_type",
        ),
        "aggregate_id": _required_text(
            row.get("aggregate_id"),
            "outbox aggregate_id",
        ),
        "event_type": _required_text(row.get("event_type"), "outbox event_type"),
        "payload": payload,
        "payload_sha256": hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest(),
        "status": status,
        "error_message": error_message,
        "attempt": int(row.get("attempts") or 0),
    }


def _healthy_components(
    status: Mapping[str, Any],
    *,
    canon_id: str,
) -> dict[str, Mapping[str, Any]]:
    components = {
        str(item.get("projection_kind") or ""): item
        for item in status.get("components") or []
        if isinstance(item, Mapping)
    }
    return {
        kind: item
        for kind, item in components.items()
        if kind
        and item.get("healthy") is True
        and item.get("status") == "healthy"
        and item.get("target_canon_commit_id") == canon_id
        and item.get("projected_canon_commit_id") == canon_id
        and int(item.get("projected_chapter_number") or 0)
        >= int(item.get("target_chapter_number") or 0)
    }


def normalize_qdrant_projection(
    *,
    status: Mapping[str, Any],
    points: Sequence[Mapping[str, Any]],
    project_id: str,
    canon_id: str,
    collection: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    collection = _required_text(collection, "Qdrant collection")
    healthy = _healthy_components(status, canon_id=canon_id)
    if "llm_kb" not in healthy:
        raise SetupBlocked("Qdrant projection checkpoint is not converged")
    bound_points = [
        point
        for point in points
        if isinstance(point.get("payload"), Mapping)
        and point["payload"].get("project_id") == project_id
        and point["payload"].get("index_kind") == "llm_kb"
        and str(point.get("id") or "")
    ]
    if not bound_points:
        raise SetupBlocked("Qdrant projection contains no fixture-bound points")
    projections = [
        {
            "projection_type": "llm_kb",
            "identity_id": str(point["id"]),
            "canon_id": canon_id,
            "status": "converged",
            "collection": collection,
        }
        for point in bound_points
    ]
    identities = [
        {
            "collection": collection,
            "projection_type": "llm_kb",
            "point_id": str(point["id"]),
            "canon_id": canon_id,
        }
        for point in bound_points
    ]
    return projections, identities


def normalize_projection_identities(
    *,
    status: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    project_id: str,
    canon_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    healthy = _healthy_components(status, canon_id=canon_id)
    bound_rows = [
        row
        for row in rows
        if row.get("project_id") == project_id
        and row.get("projected_canon_commit_id") == canon_id
        and str(row.get("projection_type") or "") in healthy
        and str(row.get("projection_id") or "")
    ]
    if not bound_rows:
        raise SetupBlocked("projection consumer identities are not converged")
    projections = [
        {
            "projection_type": str(row["projection_type"]),
            "identity_id": str(row["projection_id"]),
            "canon_id": canon_id,
            "status": "converged",
        }
        for row in bound_rows
    ]
    identities = [
        {
            "projection_type": str(row["projection_type"]),
            "projection_id": str(row["projection_id"]),
            "canon_id": canon_id,
        }
        for row in bound_rows
    ]
    return projections, identities


def snapshot_envelope(
    *,
    source_sha: str,
    fault_kind: str,
    fault_id: str,
    stage: str,
    fixture: Mapping[str, Any],
) -> dict[str, Any]:
    if SHA_PATTERN.fullmatch(str(source_sha or "")) is None:
        raise RunnerError("source SHA is not canonical")
    if fault_kind not in SUPPORTED_FAULTS:
        raise RunnerError(f"unsupported Task 4 fault: {fault_kind}")
    if stage not in {"before", "during", "after"}:
        raise RunnerError(f"unsupported snapshot stage: {stage}")
    validate_fault_id(fault_id)
    return {
        "schema_version": 2,
        "source_sha": source_sha,
        "fault_kind": fault_kind,
        "fault_id": fault_id,
        "stage": stage,
        "state": {
            "target": {"fixture": dict(fixture)},
            "mcp": {},
            "api": {},
            "database": {},
            "external": {},
            "barrier": {},
        },
    }


TASK_SQL = """
    SELECT
        id AS task_id,
        lease_epoch,
        project_id,
        status
    FROM generation_tasks
    WHERE id = %s AND project_id = %s AND deleted_at IS NULL
    /* task4 task */
"""

CANON_SQL = """
    SELECT
        commits.id AS canon_id,
        commits.idempotency_key AS natural_key,
        commits.project_id,
        commits.chapter_number,
        commits.candidate_id,
        candidates.chapter_plan_id AS chapter_id,
        candidates.version AS canon_version,
        candidates.body_hash AS content_sha256
    FROM canon_commit_records AS commits
    JOIN candidate_draft_records AS candidates
      ON candidates.id = commits.candidate_id
    WHERE commits.project_id = %s
      AND commits.chapter_number = %s
      AND commits.status = 'committed'
    ORDER BY commits.id
    /* task4 canon */
"""

ACCEPTED_SQL = """
    SELECT
        commits.id AS bundle_id,
        candidates.id AS candidate_id,
        commits.project_id,
        candidates.chapter_plan_id AS chapter_id,
        candidates.body_hash AS content_sha256
    FROM canon_commit_records AS commits
    JOIN candidate_draft_records AS candidates
      ON candidates.id = commits.candidate_id
    WHERE commits.project_id = %s
      AND commits.chapter_number = %s
      AND commits.status = 'committed'
      AND candidates.status = 'accepted'
    ORDER BY commits.id
    /* task4 accepted bundle */
"""

PROJECTION_OUTBOX_SQL = """
    SELECT
        event_id,
        aggregate_type,
        aggregate_id,
        event_type,
        payload_json,
        attempts,
        status,
        error_message
    FROM outbox_events
    WHERE event_type = 'canon.projection.requested'
      AND payload_json::jsonb ->> 'project_id' = %s
      AND (payload_json::jsonb ->> 'chapter_number')::integer = %s
    ORDER BY created_at, id
    /* task4 projection outbox */
"""

PROJECTION_IDENTITIES_SQL = """
    SELECT
        id AS projection_id,
        projection_kind AS projection_type,
        project_id,
        projected_canon_commit_id
    FROM projection_checkpoints
    WHERE project_id = %s
    ORDER BY projection_kind, id
    /* task4 projection identities */
"""

FIXTURE_BOUNDARY_SQL = """
    SELECT
        chapters.id AS chapter_id,
        candidates.id AS candidate_id,
        candidates.status AS candidate_status,
        tasks.id AS task_id
    FROM chapter_plans AS chapters
    JOIN generation_tasks AS tasks
      ON tasks.project_id = chapters.project_id
    LEFT JOIN LATERAL (
        SELECT candidate.id, candidate.status
        FROM candidate_draft_records AS candidate
        WHERE candidate.project_id = chapters.project_id
          AND candidate.chapter_number = chapters.chapter_number
        ORDER BY candidate.version DESC, candidate.created_at DESC
        LIMIT 1
    ) AS candidates ON TRUE
    WHERE chapters.project_id = %s
      AND chapters.chapter_number = %s
      AND tasks.id = %s
    /* task4 fixture boundary */
"""


class RowSource(Protocol):
    def fetch_all(
        self,
        statement: str,
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]: ...


class PsycopgRowSource:
    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[[str], Any] = psycopg_connect,
    ) -> None:
        self.database_url = normalize_database_url(database_url)
        self.connect = connect

    def fetch_all(
        self,
        statement: str,
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        connection = self.connect(self.database_url)
        try:
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(statement, tuple(params))
                return [dict(row) for row in cursor.fetchall()]
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()


@dataclass(frozen=True, slots=True)
class FixtureContext:
    fixture_id: str
    fault_id: str
    project_id: str
    chapter_number: int
    chapter_id: str
    task_id: str
    candidate_id: str = ""
    canon_id: str = ""

    def evaluator_identity(self) -> dict[str, str]:
        return {
            "fixture_id": self.fixture_id,
            "fault_id": self.fault_id,
            "resource_type": "chapter",
            "resource_id": self.chapter_id,
        }


class SQLCollector:
    def __init__(self, source: RowSource) -> None:
        self.source = source

    def _task_row(self, fixture: FixtureContext) -> dict[str, Any]:
        rows = self.source.fetch_all(
            TASK_SQL,
            (fixture.task_id, fixture.project_id),
        )
        if len(rows) != 1:
            raise SetupBlocked("fixture task identity is missing or duplicated")
        return rows[0]

    def _canon_rows(self, fixture: FixtureContext) -> list[dict[str, Any]]:
        return normalize_canon_records(
            self.source.fetch_all(
                CANON_SQL,
                (fixture.project_id, fixture.chapter_number),
            )
        )

    def _accepted_rows(self, fixture: FixtureContext) -> list[dict[str, Any]]:
        return normalize_accepted_bundles(
            self.source.fetch_all(
                ACCEPTED_SQL,
                (fixture.project_id, fixture.chapter_number),
            )
        )

    def _outbox_row(self, fixture: FixtureContext) -> dict[str, Any]:
        rows = self.source.fetch_all(
            PROJECTION_OUTBOX_SQL,
            (fixture.project_id, fixture.chapter_number),
        )
        if len(rows) != 1:
            raise SetupBlocked(
                "projection outbox identity is missing or duplicated"
            )
        return normalize_outbox_record(rows[0])

    def generation_snapshot(
        self,
        *,
        source_sha: str,
        fault_kind: str,
        stage: str,
        fixture: FixtureContext,
    ) -> dict[str, Any]:
        snapshot = snapshot_envelope(
            source_sha=source_sha,
            fault_kind=fault_kind,
            fault_id=fixture.fault_id,
            stage=stage,
            fixture=fixture.evaluator_identity(),
        )
        database = snapshot["state"]["database"]
        database["task"] = normalize_task(self._task_row(fixture))
        if stage in {"during", "after"}:
            database["canon_commits"] = self._canon_rows(fixture)
        if (
            fault_kind == "generation_worker_postcommit_crash"
            and stage in {"during", "after"}
        ):
            database["accepted_bundles"] = self._accepted_rows(fixture)
        if stage == "after":
            database["authoritative_identities"] = [
                {
                    "entity_type": "canon",
                    "record_id": row["canon_id"],
                    "project_id": row["project_id"],
                    "chapter_id": row["chapter_id"],
                    "natural_key": row["natural_key"],
                }
                for row in database["canon_commits"]
            ]
        return snapshot

    def projection_snapshot(
        self,
        *,
        source_sha: str,
        fault_kind: str,
        stage: str,
        fixture: FixtureContext,
    ) -> dict[str, Any]:
        snapshot = snapshot_envelope(
            source_sha=source_sha,
            fault_kind=fault_kind,
            fault_id=fixture.fault_id,
            stage=stage,
            fixture=fixture.evaluator_identity(),
        )
        database = snapshot["state"]["database"]
        database["canon_commits"] = self._canon_rows(fixture)
        database["outbox"] = self._outbox_row(fixture)
        return snapshot

    def projection_identity_rows(
        self,
        fixture: FixtureContext,
    ) -> list[dict[str, Any]]:
        return self.source.fetch_all(
            PROJECTION_IDENTITIES_SQL,
            (fixture.project_id,),
        )

    def wait_task_fixture(
        self,
        *,
        fault_id: str,
        fixture_id: str,
        project_id: str,
        task_id: str,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.5,
    ) -> FixtureContext:
        return self._wait_fixture_boundary(
            fault_id=fault_id,
            fixture_id=fixture_id,
            project_id=project_id,
            task_id=task_id,
            require_review_ready=False,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )

    def wait_review_ready(
        self,
        *,
        fault_id: str,
        fixture_id: str,
        project_id: str,
        task_id: str,
        timeout_seconds: float = 600.0,
        poll_seconds: float = 1.0,
    ) -> FixtureContext:
        return self._wait_fixture_boundary(
            fault_id=fault_id,
            fixture_id=fixture_id,
            project_id=project_id,
            task_id=task_id,
            require_review_ready=True,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )

    def _wait_fixture_boundary(
        self,
        *,
        fault_id: str,
        fixture_id: str,
        project_id: str,
        task_id: str,
        require_review_ready: bool,
        timeout_seconds: float,
        poll_seconds: float,
    ) -> FixtureContext:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        first = True
        while first or time.monotonic() < deadline:
            first = False
            rows = self.source.fetch_all(
                FIXTURE_BOUNDARY_SQL,
                (project_id, 1, task_id),
            )
            if len(rows) == 1:
                row = rows[0]
                chapter_id = str(row.get("chapter_id") or "")
                candidate_id = str(row.get("candidate_id") or "")
                ready = row.get("candidate_status") == "ready_for_canon"
                if chapter_id and (not require_review_ready or (candidate_id and ready)):
                    return FixtureContext(
                        fixture_id=fixture_id,
                        fault_id=fault_id,
                        project_id=project_id,
                        chapter_number=1,
                        chapter_id=chapter_id,
                        task_id=task_id,
                        candidate_id=candidate_id,
                    )
            if time.monotonic() < deadline:
                time.sleep(poll_seconds)
        boundary = "review-ready candidate" if require_review_ready else "chapter fixture"
        raise SetupBlocked(f"one-chapter fixture did not reach {boundary}")

    def wait_task_reclaimed(
        self,
        fixture: FixtureContext,
        *,
        previous_lease_epoch: int,
        timeout_seconds: float = 420.0,
        poll_seconds: float = 1.0,
    ) -> FixtureContext:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            row = self._task_row(fixture)
            if (
                row.get("status") == "completed"
                and int(row.get("lease_epoch") or 0) > previous_lease_epoch
            ):
                return fixture
            time.sleep(poll_seconds)
        raise SetupBlocked(
            "generation task was not reclaimed and completed at a higher lease epoch"
        )

    def wait_projection_base(
        self,
        fixture: FixtureContext,
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.5,
    ) -> FixtureContext:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                canon = self._canon_rows(fixture)
                self._outbox_row(fixture)
            except SetupBlocked:
                canon = []
            if len(canon) == 1:
                return replace(fixture, canon_id=canon[0]["canon_id"])
            time.sleep(poll_seconds)
        raise SetupBlocked("Canon/outbox durable boundary was not observed")

    def wait_outbox_processed(
        self,
        fixture: FixtureContext,
        *,
        timeout_seconds: float = 600.0,
        poll_seconds: float = 1.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            rows = self.source.fetch_all(
                PROJECTION_OUTBOX_SQL,
                (fixture.project_id, fixture.chapter_number),
            )
            if len(rows) == 1 and rows[0].get("status") == "processed":
                return
            time.sleep(poll_seconds)
        raise SetupBlocked("projection outbox did not converge to processed")

    def wait_qdrant_failure(
        self,
        fixture: FixtureContext,
        *,
        baseline_attempt: int,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        first = True
        while first or time.monotonic() < deadline:
            first = False
            outbox = self._outbox_row(fixture)
            if (
                outbox["status"] == "pending"
                and bool(outbox["error_message"])
                and outbox["attempt"] > int(baseline_attempt)
            ):
                return outbox
            if time.monotonic() < deadline:
                time.sleep(poll_seconds)
        raise SetupBlocked(
            "fixture outbox did not record a failed Qdrant attempt"
        )

class ForWinAPI:
    def __init__(
        self,
        *,
        api_url: str,
        transport: Callable[..., dict[str, Any]] = http_json,
    ) -> None:
        self.api_url = required_url(api_url, "API URL")
        self.transport = transport

    def _project_path(self, project_id: str, suffix: str) -> str:
        encoded_project = urllib.parse.quote(
            _required_text(project_id, "project_id"),
            safe="",
        )
        return f"{self.api_url}/api/projects/{encoded_project}/{suffix}"

    def approve_chapter(
        self,
        project_id: str,
        chapter_number: int,
    ) -> dict[str, Any]:
        payload = self.transport(
            "POST",
            self._project_path(
                project_id,
                f"chapters/{int(chapter_number)}/review/approve",
            ),
            query=None,
            json_body={
                "continue_generation": False,
                "reason": "fault-local recovery evidence acceptance",
            },
        )
        if payload.get("ok") is False or payload.get("status") != "accepted":
            raise SetupBlocked(
                "supported chapter approval did not reach accepted: "
                f"{payload.get('status') or payload}"
            )
        return payload

    def projection_status(self, project_id: str) -> dict[str, Any]:
        return self.transport(
            "GET",
            self._project_path(project_id, "projections/status"),
            query=None,
            json_body=None,
        )

    def refresh_projection(
        self,
        project_id: str,
        chapter_number: int,
    ) -> dict[str, Any]:
        payload = self.transport(
            "POST",
            self._project_path(project_id, "projections/refresh"),
            query={
                "projection_kind": "all",
                "as_of_chapter": int(chapter_number),
                "force": True,
                "defer": False,
            },
            json_body=None,
        )
        if payload.get("ok") is False:
            raise SetupBlocked("projection refresh replay failed")
        return payload

    def wait_projection_converged(
        self,
        project_id: str,
        canon_id: str,
        *,
        timeout_seconds: float = 600.0,
        poll_seconds: float = 1.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = self.projection_status(project_id)
            healthy = _healthy_components(latest, canon_id=canon_id)
            components = {
                str(item.get("projection_kind") or "")
                for item in latest.get("components") or []
                if isinstance(item, Mapping)
            }
            if components and set(healthy) == components:
                return latest
            time.sleep(poll_seconds)
        raise SetupBlocked(
            "projection API did not converge for the fixture Canon identity"
        )


class QdrantReader:
    def __init__(
        self,
        *,
        qdrant_url: str,
        collection: str,
        transport: Callable[..., dict[str, Any]] = http_json,
    ) -> None:
        self.qdrant_url = required_url(qdrant_url, "Qdrant URL")
        self.collection = _required_text(collection, "Qdrant collection")
        self.transport = transport

    def project_points(self, project_id: str) -> list[dict[str, Any]]:
        collection = urllib.parse.quote(self.collection, safe="")
        url = f"{self.qdrant_url}/collections/{collection}/points/scroll"
        offset: Any = None
        seen_offsets: set[str] = set()
        points: list[dict[str, Any]] = []
        while True:
            body: dict[str, Any] = {
                "filter": {
                    "must": [
                        {
                            "key": "project_id",
                            "match": {"value": project_id},
                        }
                    ]
                },
                "limit": 256,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            payload = self.transport(
                "POST",
                url,
                query=None,
                json_body=body,
            )
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise SetupBlocked("Qdrant scroll returned no result object")
            batch = result.get("points") or []
            if not isinstance(batch, list) or any(
                not isinstance(item, Mapping) for item in batch
            ):
                raise SetupBlocked("Qdrant scroll returned malformed points")
            points.extend(dict(item) for item in batch)
            offset = result.get("next_page_offset")
            if offset is None:
                return points
            key = canonical_json(offset)
            if key in seen_offsets:
                raise SetupBlocked("Qdrant scroll repeated a page offset")
            seen_offsets.add(key)


@dataclass(frozen=True, slots=True)
class LiveRunResult:
    status: str
    report_path: Path


class LiveRunner:
    def __init__(
        self,
        *,
        fault_kind: str,
        fault_id: str,
        source_sha: str,
        controller: Any,
        lifecycle: Any,
        sql_collector: Any,
        api: Any,
        qdrant: Any,
        writer: Any,
        barrier_factory: Callable[[], Any] | None = None,
    ) -> None:
        if fault_kind not in SUPPORTED_FAULTS:
            raise RunnerError(f"unsupported Task 4 fault: {fault_kind}")
        self.fault_kind = fault_kind
        self.fault_id = validate_fault_id(fault_id)
        self.source_sha = source_sha
        self.controller = controller
        self.lifecycle = lifecycle
        self.sql = sql_collector
        self.api = api
        self.qdrant = qdrant
        self.writer = writer
        self.barrier_factory = barrier_factory
        self.stage = "initial"
        self.stack_started = False
        self.faulted = False
        self.recovered = False
        self.barrier: Any | None = None
        self.barrier_observation: BarrierObservation | None = None
        self.fixture: FixtureContext | None = None
        self.supplemental_artifacts: dict[str, dict[str, Any]] = {}

    def run(self) -> LiveRunResult:
        snapshots: dict[str, dict[str, Any]] | None = None
        failure: BaseException | None = None
        cleanup_errors: list[str] = []
        try:
            self.stage = "fresh_up"
            self.controller.fresh_up(self.fault_id)
            self.stack_started = True
            if self.fault_kind in GENERATION_FAULTS:
                snapshots = self._run_generation_fault()
            else:
                snapshots = self._run_projection_fault()
        except BaseException as exc:
            failure = exc
        finally:
            if (
                self.barrier is not None
                and self.stack_started
                and not self.faulted
            ):
                try:
                    self.controller.stop("generation-worker", self.fault_id)
                    self.faulted = True
                except BaseException as exc:
                    cleanup_errors.append(
                        f"generation worker quiesce: {exc}"
                    )
            if self.barrier is not None:
                try:
                    self.barrier.cleanup()
                except BaseException as exc:
                    cleanup_errors.append(f"barrier cleanup: {exc}")
                else:
                    try:
                        self._capture_barrier_evidence()
                    except BaseException as exc:
                        cleanup_errors.append(
                            f"barrier evidence: {exc}"
                        )
            cleanup_errors.extend(self._cleanup_stack())

        if failure is None and cleanup_errors:
            failure = RunnerError("; ".join(cleanup_errors))
            self.stage = "cleanup"
        if failure is not None:
            report_path = self.writer.write_setup_blocked(
                fault_kind=self.fault_kind,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                failure_stage=self.stage,
                failure_reason=str(failure) or type(failure).__name__,
                cleanup_errors=cleanup_errors,
            )
            return LiveRunResult(
                status="setup_blocked",
                report_path=report_path,
            )
        if snapshots is None:
            raise RunnerError("live runner produced no snapshots")
        self.stage = "evidence_write"
        report_path = self.writer.write_pass_report(
            fault_kind=self.fault_kind,
            fault_id=self.fault_id,
            source_sha=self.source_sha,
            snapshots=snapshots,
            event_log_path=self.controller.event_log_path,
            supplemental_artifacts=self.supplemental_artifacts,
        )
        return LiveRunResult(status="pass", report_path=report_path)

    def _run_generation_fault(self) -> dict[str, dict[str, Any]]:
        self.stage = "genesis"
        project = asyncio.run(self.lifecycle.create_genesis_project())
        if self.barrier_factory is None:
            raise SetupBlocked("generation barrier factory is missing")
        self.stage = "barrier_install"
        self.barrier = self.barrier_factory()
        self.barrier.install(project_id=project.project_id, chapter_number=1)
        self.stage = "writing_handoff"
        task = asyncio.run(self.lifecycle.start_writing(project.project_id))
        fixture = self.sql.wait_task_fixture(
            fault_id=self.fault_id,
            fixture_id=f"fixture-{self.fault_id}",
            project_id=project.project_id,
            task_id=task.task_id,
        )
        before = self.sql.generation_snapshot(
            source_sha=self.source_sha,
            fault_kind=self.fault_kind,
            stage="before",
            fixture=fixture,
        )
        previous_epoch = int(
            before["state"]["database"]["task"]["lease_epoch"]
        )
        self.stage = "barrier_wait"
        self.barrier_observation = self.barrier.wait_for_blocked_waiter()
        self.fixture = fixture
        during = self.sql.generation_snapshot(
            source_sha=self.source_sha,
            fault_kind=self.fault_kind,
            stage="during",
            fixture=fixture,
        )
        self.stage = "worker_kill"
        self.controller.kill("generation-worker", self.fault_id)
        self.faulted = True
        self.stage = "barrier_cleanup"
        self.barrier.cleanup()
        self.stage = "worker_recovery"
        self.controller.start("generation-worker", self.fault_id)
        self.recovered = True
        self.stage = "task_reclaim"
        fixture = self.sql.wait_task_reclaimed(
            fixture,
            previous_lease_epoch=previous_epoch,
        )
        after = self.sql.generation_snapshot(
            source_sha=self.source_sha,
            fault_kind=self.fault_kind,
            stage="after",
            fixture=fixture,
        )
        return {"before": before, "during": during, "after": after}

    def _capture_barrier_evidence(self) -> None:
        if self.barrier_observation is None:
            return
        residue = getattr(self.barrier, "last_residue_count", 0)
        if residue is None:
            raise RunnerError("barrier cleanup residue was not measured")
        if int(residue) != 0:
            raise RunnerError(f"barrier residue count is {residue}")
        observation = self.barrier_observation
        payload: dict[str, Any] = {
            "schema_version": 1,
            "fault_kind": self.fault_kind,
            "fault_id": self.fault_id,
            "holder_pid": observation.holder_pid,
            "waiter_pid": observation.waiter_pid,
            "waiter_application_name": observation.waiter_application_name,
            "target_role": observation.target_role,
            "holder_count": 1,
            "waiter_count": 1,
            "blocking_pids": [observation.holder_pid],
            "residue_count": int(residue),
        }
        if self.fixture is not None:
            payload["fixture"] = {
                "fixture_id": self.fixture.fixture_id,
                "project_id": self.fixture.project_id,
                "chapter_number": self.fixture.chapter_number,
                "chapter_id": self.fixture.chapter_id,
                "task_id": self.fixture.task_id,
            }
        names = getattr(self.barrier, "names", None)
        if names is not None:
            payload["sql_objects"] = {
                "scope_table": names.scope_table,
                "function": names.function,
                "trigger": names.trigger,
                "target_table": self.barrier.target_table,
            }
        if hasattr(self.barrier, "advisory_key"):
            payload["advisory_lock"] = {
                "key": self.barrier.advisory_key,
                "classid": self.barrier.lock_identity[0],
                "objid": self.barrier.lock_identity[1],
            }
        self.supplemental_artifacts["barrier-observation.json"] = payload

    def _run_projection_fault(self) -> dict[str, dict[str, Any]]:
        if self.api is None:
            raise SetupBlocked("ForWin API client is missing")
        self.stage = "genesis"
        project = asyncio.run(self.lifecycle.create_genesis_project())
        self.stage = "writing_handoff"
        task = asyncio.run(self.lifecycle.start_writing(project.project_id))
        self.stage = "review_ready"
        fixture = self.sql.wait_review_ready(
            fault_id=self.fault_id,
            fixture_id=f"fixture-{self.fault_id}",
            project_id=project.project_id,
            task_id=task.task_id,
        )
        service = SERVICE_BY_FAULT[self.fault_kind]
        self.stage = "service_fault"
        self.controller.stop(service, self.fault_id)
        self.faulted = True
        self.stage = "supported_approval"
        self.api.approve_chapter(
            fixture.project_id,
            fixture.chapter_number,
        )
        self.stage = "durable_boundary"
        fixture = self.sql.wait_projection_base(fixture)
        before = self.sql.projection_snapshot(
            source_sha=self.source_sha,
            fault_kind=self.fault_kind,
            stage="before",
            fixture=fixture,
        )
        if self.fault_kind == "qdrant_unavailable":
            self.stage = "qdrant_failure"
            baseline_attempt = int(
                before["state"]["database"]["outbox"]["attempt"]
            )
            self.sql.wait_qdrant_failure(
                fixture,
                baseline_attempt=baseline_attempt,
            )
        during = self.sql.projection_snapshot(
            source_sha=self.source_sha,
            fault_kind=self.fault_kind,
            stage="during",
            fixture=fixture,
        )
        self.stage = "service_recovery"
        self.controller.start(service, self.fault_id)
        self.recovered = True
        self.stage = "projection_convergence"
        self.sql.wait_outbox_processed(fixture)
        status = self.api.wait_projection_converged(
            fixture.project_id,
            fixture.canon_id,
        )
        replay_baseline = self._collect_projection_evidence(
            status=status,
            fixture=fixture,
        )
        self.stage = "projection_replay"
        self.api.refresh_projection(
            fixture.project_id,
            fixture.chapter_number,
        )
        status = self.api.wait_projection_converged(
            fixture.project_id,
            fixture.canon_id,
        )
        after = self.sql.projection_snapshot(
            source_sha=self.source_sha,
            fault_kind=self.fault_kind,
            stage="after",
            fixture=fixture,
        )
        final = self._collect_projection_evidence(
            status=status,
            fixture=fixture,
        )
        external = after["state"]["external"]
        if self.fault_kind == "qdrant_unavailable":
            external.update(
                replay_baseline_projections=replay_baseline[0],
                replay_baseline_point_identities=replay_baseline[1],
                projections=final[0],
                point_identities=final[1],
            )
        else:
            external.update(
                replay_baseline_projections=replay_baseline[0],
                replay_baseline_projection_identities=replay_baseline[1],
                projections=final[0],
                projection_identities=final[1],
            )
        return {"before": before, "during": during, "after": after}

    def _collect_projection_evidence(
        self,
        *,
        status: Mapping[str, Any],
        fixture: FixtureContext,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self.fault_kind == "qdrant_unavailable":
            if self.qdrant is None:
                raise SetupBlocked("Qdrant reader is missing")
            return normalize_qdrant_projection(
                status=status,
                points=self.qdrant.project_points(fixture.project_id),
                project_id=fixture.project_id,
                canon_id=fixture.canon_id,
                collection=self.qdrant.collection,
            )
        return normalize_projection_identities(
            status=status,
            rows=self.sql.projection_identity_rows(fixture),
            project_id=fixture.project_id,
            canon_id=fixture.canon_id,
        )

    def _cleanup_stack(self) -> list[str]:
        if not self.stack_started:
            return []
        errors: list[str] = []
        service = SERVICE_BY_FAULT[self.fault_kind]
        try:
            if self.faulted and not self.recovered:
                self.controller.start(service, self.fault_id)
                self.recovered = True
            elif not self.faulted:
                # The controller requires a complete service lifecycle before
                # terminal destroy. This cleanup-only pair never counts as PASS.
                self.controller.stop(service, self.fault_id)
                self.faulted = True
                self.controller.start(service, self.fault_id)
                self.recovered = True
        except BaseException as exc:
            errors.append(f"service restore: {exc}")
        if self.faulted and self.recovered:
            try:
                self.controller.destroy()
            except BaseException as exc:
                errors.append(f"stack destroy: {exc}")
        return errors


@dataclass(frozen=True, slots=True)
class RunConfig:
    fault_kind: str
    fault_id: str
    candidate_manifest: Path
    source_sha: str
    mcp_url: str
    api_url: str
    database_url: str
    evidence_dir: Path
    qdrant_url: str = ""
    qdrant_collection: str = ""


def resolve_run_config(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] = os.environ,
) -> RunConfig:
    fault_kind = str(args.fault_kind or "")
    if fault_kind not in SUPPORTED_FAULTS:
        raise RunnerError(f"unsupported Task 4 fault: {fault_kind}")
    fault_id = validate_fault_id(args.fault_id)
    identity = candidate_identity(Path(args.candidate_manifest))

    database_env = str(args.database_url_env or "")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database_env) is None:
        raise RunnerError("database URL environment name is invalid")
    database_url = normalize_database_url(
        str(environ.get(database_env) or "").strip()
    )
    if not database_url:
        raise RunnerError(f"database URL environment is empty: {database_env}")
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise RunnerError("database URL must use PostgreSQL")

    evidence_dir = Path(args.evidence_dir).expanduser().resolve()
    if evidence_dir.exists() and any(evidence_dir.iterdir()):
        raise RunnerError(
            f"evidence directory is not empty; use a new directory: "
            f"{evidence_dir}"
        )
    qdrant_url = ""
    qdrant_collection = ""
    if fault_kind == "qdrant_unavailable":
        qdrant_url = str(
            environ.get("FORWIN_RECOVERY_QDRANT_URL") or ""
        ).strip()
        if not qdrant_url:
            raise RunnerError(
                "FORWIN_RECOVERY_QDRANT_URL is required for the Qdrant fault"
            )
        qdrant_url = required_url(qdrant_url, "Qdrant URL")
        qdrant_collection = str(
            environ.get("FORWIN_RECOVERY_QDRANT_COLLECTION") or ""
        ).strip()
        if not qdrant_collection:
            raise RunnerError(
                "FORWIN_RECOVERY_QDRANT_COLLECTION is required for the "
                "Qdrant fault"
            )
    return RunConfig(
        fault_kind=fault_kind,
        fault_id=fault_id,
        candidate_manifest=identity.manifest_path,
        source_sha=identity.source_sha,
        mcp_url=required_url(args.mcp_url, "MCP URL"),
        api_url=required_url(args.api_url, "API URL"),
        database_url=database_url,
        evidence_dir=evidence_dir,
        qdrant_url=qdrant_url,
        qdrant_collection=qdrant_collection,
    )


def build_live_runner(config: RunConfig) -> LiveRunner:
    controller = RecoveryController(
        candidate_manifest=config.candidate_manifest,
        evidence_dir=config.evidence_dir,
    )
    lifecycle = OneChapterLifecycle(
        mcp_url=config.mcp_url,
        fault_id=config.fault_id,
    )
    collector = SQLCollector(PsycopgRowSource(config.database_url))
    api = ForWinAPI(api_url=config.api_url)
    qdrant = (
        QdrantReader(
            qdrant_url=config.qdrant_url,
            collection=config.qdrant_collection,
        )
        if config.fault_kind == "qdrant_unavailable"
        else None
    )
    barrier_factory: Callable[[], AdvisoryBarrier] | None = None
    if config.fault_kind in GENERATION_FAULTS:
        barrier_factory = lambda: AdvisoryBarrier(
            kind=config.fault_kind,
            fault_id=config.fault_id,
            database_url=config.database_url,
        )
    return LiveRunner(
        fault_kind=config.fault_kind,
        fault_id=config.fault_id,
        source_sha=config.source_sha,
        controller=controller,
        lifecycle=lifecycle,
        sql_collector=collector,
        api=api,
        qdrant=qdrant,
        writer=EvidenceWriter(evidence_dir=config.evidence_dir),
        barrier_factory=barrier_factory,
    )



def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run fault-local generation and projection recovery proofs."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument(
        "--fault-kind",
        choices=SUPPORTED_FAULTS,
        required=True,
    )
    run_parser.add_argument("--fault-id", required=True)
    run_parser.add_argument("--candidate-manifest", type=Path, required=True)
    run_parser.add_argument("--mcp-url", required=True)
    run_parser.add_argument("--api-url", required=True)
    run_parser.add_argument("--database-url-env", required=True)
    run_parser.add_argument("--evidence-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command != "run":
        raise RunnerError(f"unsupported command: {args.command}")
    result = build_live_runner(resolve_run_config(args)).run()
    print(result.report_path)
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
