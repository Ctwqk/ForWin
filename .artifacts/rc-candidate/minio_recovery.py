#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from psycopg import sql


ARTIFACT_DIR = Path(__file__).resolve().parent
if str(ARTIFACT_DIR) not in sys.path:
    sys.path.insert(0, str(ARTIFACT_DIR))
import recovery_runner_common as common
from recovery_runner_common import (
    EvidenceWriter,
    OneChapterLifecycle,
    RecoveryController,
    RunnerError,
    SetupBlocked,
    atomic_write_json_new,
    candidate_identity,
    canonical_digest,
    canonical_json,
    http_json,
    normalize_database_url,
    psycopg_connect,
    required_text,
    required_url,
    stable_hash,
    validate_fault_id,
)


SUPPORTED_FAULTS = (
    "minio_pre_canon_unavailable",
    "minio_post_canon_unavailable",
)
MINIO_SERVICE = "minio"
OUTBOX_SERVICE = "outbox-worker"
PRE_APPROVAL_HOLD = "pre-approval"
SAME_EVENT_REPLAY_HOLD = "same-event-replay"
PHASE3_EVENT_TYPE = "canon.phase3.requested"
POST_CANON_STEPS = ("planning", "arc", "world", "feedback")
SQL_IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")
SAFE_KEY_PART_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ApprovalTransportFailure(RunnerError):
    pass


def _json_payload(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SetupBlocked(f"{field} is invalid JSON") from exc
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise SetupBlocked(f"{field} is missing")
    if not isinstance(payload, dict):
        raise SetupBlocked(f"{field} is not an object")
    return payload


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_json_safe(nested) for nested in value]
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _required_counter(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise SetupBlocked(f"{field} is not a nonnegative integer")
    return value


def snapshot_envelope(
    *,
    source_sha: str,
    fault_kind: str,
    fault_id: str,
    stage: str,
    fixture: Mapping[str, Any],
    endpoint_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", str(source_sha or "")) is None:
        raise RunnerError("source SHA is not canonical")
    if fault_kind not in SUPPORTED_FAULTS:
        raise RunnerError(f"unsupported Task 5 fault: {fault_kind}")
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
            "target": {
                "fixture": dict(fixture),
                **(
                    {"endpoint_identity": dict(endpoint_identity)}
                    if endpoint_identity is not None
                    else {}
                ),
            },
            "mcp": {},
            "api": {},
            "database": {},
            "external": {},
            "barrier": {},
        },
    }


def normalize_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": required_text(row.get("candidate_id"), "candidate_id"),
        "project_id": required_text(row.get("project_id"), "project_id"),
        "chapter_id": required_text(row.get("chapter_id"), "chapter_id"),
        "content_sha256": canonical_digest(
            row.get("content_sha256"),
            "candidate content_sha256",
        ),
    }


def normalize_canon_records(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "canon_id": required_text(row.get("canon_id"), "canon_id"),
            "natural_key": required_text(
                row.get("natural_key"),
                "canon natural_key",
            ),
            "project_id": required_text(row.get("project_id"), "project_id"),
            "chapter_id": required_text(row.get("chapter_id"), "chapter_id"),
            "chapter_number": int(row.get("chapter_number") or 0),
            "candidate_id": required_text(
                row.get("candidate_id"),
                "candidate_id",
            ),
            "canon_version": int(row.get("canon_version") or 0),
            "content_sha256": canonical_digest(
                row.get("content_sha256"),
                "canon content_sha256",
            ),
        }
        for row in rows
    ]


def normalize_accepted_bundles(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "bundle_id": required_text(row.get("bundle_id"), "bundle_id"),
            "candidate_id": required_text(
                row.get("candidate_id"),
                "candidate_id",
            ),
            "project_id": required_text(row.get("project_id"), "project_id"),
            "chapter_id": required_text(row.get("chapter_id"), "chapter_id"),
            "content_sha256": canonical_digest(
                row.get("content_sha256"),
                "accepted content_sha256",
            ),
        }
        for row in rows
    ]


def normalize_maintenance(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "natural_key": required_text(
            row.get("natural_key"),
            "maintenance natural_key",
        ),
        "project_id": required_text(row.get("project_id"), "project_id"),
        "canon_id": required_text(row.get("canon_id"), "canon_id"),
        "attempt": int(row.get("attempt") or 0),
        "lease_epoch": int(row.get("lease_epoch") or 0),
    }


def normalize_authoritative(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "entity_type": required_text(
                row.get("entity_type"),
                "authoritative entity_type",
            ),
            "record_id": required_text(
                row.get("record_id"),
                "authoritative record_id",
            ),
            "project_id": required_text(row.get("project_id"), "project_id"),
            "chapter_id": required_text(row.get("chapter_id"), "chapter_id"),
            "natural_key": required_text(
                row.get("natural_key"),
                "authoritative natural_key",
            ),
        }
        for row in rows
    ]


def expected_world_object_key(
    *,
    project_id: str,
    canon_id: str,
    prefix: str,
) -> str:
    project = required_text(project_id, "project_id")
    commit = required_text(canon_id, "canon_id")
    if SAFE_KEY_PART_PATTERN.fullmatch(commit) is None:
        raise SetupBlocked("canon_id is not safe for a production artifact key")
    artifact_key = PurePosixPath(
        "post_canon",
        commit,
        "world",
        "llm_trace.json",
    ).as_posix()
    relative = PurePosixPath(
        "projects",
        project,
        "keyed",
        artifact_key,
    ).as_posix()
    normalized_prefix = str(prefix or "").strip("/")
    return (
        PurePosixPath(normalized_prefix, relative).as_posix()
        if normalized_prefix
        else relative
    )


class MinioInventory:
    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        prefix: str,
        endpoint_url: str,
    ) -> None:
        self.client = client
        self.bucket = required_text(bucket, "MinIO bucket")
        self.prefix = str(prefix or "").strip("/")
        self.endpoint_url = required_url(endpoint_url, "MinIO URL")

    def project_objects(self, project_id: str) -> list[dict[str, Any]]:
        project = required_text(project_id, "project_id")
        project_prefix = PurePosixPath(
            self.prefix,
            "projects",
            project,
        ).as_posix().lstrip("/")
        listed = list(
            self.client.list_objects(
                self.bucket,
                prefix=project_prefix,
                recursive=True,
            )
        )
        keys = [
            required_text(
                getattr(item, "object_name", ""),
                "MinIO listed object key",
            )
            for item in listed
        ]
        if len(keys) != len(set(keys)):
            raise SetupBlocked("MinIO list returned duplicate object keys")
        return [self._object(key) for key in sorted(keys)]

    def require_world_object(
        self,
        *,
        project_id: str,
        canon_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        expected = expected_world_object_key(
            project_id=project_id,
            canon_id=canon_id,
            prefix=self.prefix,
        )
        inventory = self.project_objects(project_id)
        matches = [row for row in inventory if row["key"] == expected]
        if len(matches) != 1:
            raise SetupBlocked(
                "expected production world trace object was not observed exactly once"
            )
        return matches[0], inventory

    def _object(self, key: str) -> dict[str, Any]:
        head = self.client.stat_object(self.bucket, key)
        head_key = required_text(
            getattr(head, "object_name", key),
            "MinIO HEAD object key",
        )
        if head_key != key:
            raise SetupBlocked("MinIO list and HEAD object keys differ")
        response = self.client.get_object(self.bucket, key)
        try:
            body = response.read()
            if not isinstance(body, (bytes, bytearray)):
                raise SetupBlocked("MinIO object response is not bytes")
            content = bytes(body)
        finally:
            response.close()
            response.release_conn()
        size = int(getattr(head, "size", -1))
        if size != len(content):
            raise SetupBlocked("MinIO HEAD size does not match object bytes")
        etag = required_text(getattr(head, "etag", ""), "MinIO etag").strip('"')
        content_type = required_text(
            getattr(head, "content_type", ""),
            "MinIO content_type",
        )
        return {
            "key": key,
            "etag": etag,
            "size": size,
            "content_type": content_type,
            "content_sha256": hashlib.sha256(content).hexdigest(),
        }

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
            return
        clear = getattr(getattr(self.client, "_http", None), "clear", None)
        if callable(clear):
            clear()


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    method: str
    url: str
    body: dict[str, Any]
    candidate_id: str
    identity_sha256: str

    @property
    def reason(self) -> str:
        return str(self.body["reason"])

    @classmethod
    def for_fixture(
        cls,
        *,
        api_url: str,
        project_id: str,
        chapter_number: int,
        fault_id: str,
        candidate_id: str,
    ) -> "ApprovalRequest":
        base = required_url(api_url, "API URL")
        project = urllib.parse.quote(
            required_text(project_id, "project_id"),
            safe="",
        )
        chapter = int(chapter_number or 0)
        if chapter < 1:
            raise RunnerError("chapter_number must be positive")
        fault = validate_fault_id(fault_id)
        candidate = required_text(candidate_id, "candidate_id")
        body = {
            "continue_generation": False,
            "reason": f"recovery-evidence:{fault}:{candidate}",
        }
        url = (
            f"{base}/api/projects/{project}/chapters/{chapter}/review/approve"
        )
        identity = {
            "method": "POST",
            "url": url,
            "body": body,
            "candidate_id": candidate,
        }
        return cls(
            method="POST",
            url=url,
            body=body,
            candidate_id=candidate,
            identity_sha256=stable_hash(identity),
        )


class ApprovalAPI:
    def __init__(
        self,
        *,
        api_url: str = "",
        transport: Callable[..., dict[str, Any]] = http_json,
    ) -> None:
        self.api_url = required_url(api_url, "API URL") if api_url else ""
        self.transport = transport

    def send(self, request: ApprovalRequest) -> dict[str, Any]:
        try:
            payload = self.transport(
                request.method,
                request.url,
                query=None,
                json_body=request.body,
            )
        except ApprovalTransportFailure:
            raise
        except Exception as exc:
            raise ApprovalTransportFailure(
                f"supported approval request failed: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SetupBlocked("approval response is not an object")
        result = dict(payload)
        if result.get("ok") is False:
            raise ApprovalTransportFailure(
                f"supported approval returned failure: {result}"
            )
        if result.get("status") not in {"accepted", "maintenance_pending"}:
            raise SetupBlocked(
                "supported approval did not accept Canon: "
                f"{result.get('status') or result}"
            )
        return result


class AsyncApproval:
    def __init__(self, api: ApprovalAPI, request: ApprovalRequest) -> None:
        self.api = api
        self.request = request
        self.result: dict[str, Any] | None = None
        self.error: Exception | None = None
        self.thread = threading.Thread(
            target=self._target,
            name=f"task5-approval-{request.identity_sha256[:12]}",
            daemon=False,
        )

    def _target(self) -> None:
        try:
            self.result = self.api.send(self.request)
        except Exception as exc:
            self.error = exc

    def start(self) -> None:
        self.thread.start()

    def require_blocked(self) -> None:
        if not self.thread.is_alive():
            detail = self.error or self.result or "completed"
            raise SetupBlocked(
                f"approval request was not blocked at review_approved: {detail}"
            )

    def join(self, *, timeout_seconds: float = 120.0) -> dict[str, Any]:
        self.thread.join(timeout=max(0.0, timeout_seconds))
        if self.thread.is_alive():
            raise SetupBlocked("approval request thread did not finish")
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise SetupBlocked("approval request produced no result")
        return self.result


@dataclass(frozen=True, slots=True)
class BarrierNames:
    scope_table: str
    function: str
    trigger: str


def barrier_names(fault_id: str) -> BarrierNames:
    validate_fault_id(fault_id)
    digest = hashlib.sha256(
        f"task5-review:{fault_id}".encode("ascii")
    ).hexdigest()[:16]
    names = BarrierNames(
        scope_table=f"fw_minio_{digest}_scope",
        function=f"fw_minio_{digest}_fn",
        trigger=f"fw_minio_{digest}_trg",
    )
    if any(
        SQL_IDENTIFIER_PATTERN.fullmatch(value) is None
        for value in (names.scope_table, names.function, names.trigger)
    ):
        raise RunnerError("generated review barrier identifier is unsafe")
    return names


def advisory_key(fault_id: str) -> int:
    validate_fault_id(fault_id)
    unsigned = int.from_bytes(
        hashlib.sha256(
            f"task5-review-lock:{fault_id}".encode("ascii")
        ).digest()[:8],
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
    holder_count: int
    waiter_count: int
    canon_count: int
    waiter_application_name: str
    database_role: str
    backend_type: str


class ReviewApprovedBarrier:
    def __init__(
        self,
        *,
        fault_id: str,
        database_url: str,
        connect: Callable[[str], Any] = psycopg_connect,
    ) -> None:
        self.fault_id = validate_fault_id(fault_id)
        self.database_url = normalize_database_url(database_url)
        self.connect = connect
        self.names = barrier_names(fault_id)
        self.advisory_key = advisory_key(fault_id)
        self.lock_identity = advisory_lock_identity(self.advisory_key)
        self.holder_application_name = (
            f"{self.names.scope_table[:45]}_holder"
        )
        self.waiter_application_name = (
            f"{self.names.scope_table[:45]}_api"
        )
        self.database_role = ""
        self._admin: Any | None = None
        self._holder: Any | None = None
        self._released = False
        self.residue: dict[str, int] | None = None

    def install(self, *, request: ApprovalRequest) -> None:
        if self._admin is not None or self._holder is not None:
            raise RunnerError("review barrier is already installed")
        self._admin = self.connect(self.database_url)
        self._admin.autocommit = True
        try:
            self._holder = self.connect(self.database_url)
            self._holder.autocommit = True
            with self._admin.cursor() as cursor:
                cursor.execute(
                    "SELECT current_user AS database_role "
                    "/* task5 barrier current_user */"
                )
                row = cursor.fetchone() or {}
            self.database_role = required_text(
                row.get("database_role"),
                "barrier database role",
            )
            if self._object_residue_count() != 0:
                raise SetupBlocked("fault-scoped review barrier objects exist")
            project_id, chapter_number = self._request_scope(request)
            with self._admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "CREATE UNLOGGED TABLE {} ("
                        "project_id text NOT NULL, "
                        "chapter_number integer NOT NULL, "
                        "reason text NOT NULL, "
                        "advisory_key bigint NOT NULL, "
                        "waiter_application_name text NOT NULL, "
                        "PRIMARY KEY (project_id, chapter_number)"
                        ")"
                    ).format(sql.Identifier(self.names.scope_table))
                )
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {} "
                        "(project_id, chapter_number, reason, advisory_key, "
                        "waiter_application_name) VALUES (%s, %s, %s, %s, %s)"
                    ).format(sql.Identifier(self.names.scope_table)),
                    (
                        project_id,
                        chapter_number,
                        request.reason,
                        self.advisory_key,
                        self.waiter_application_name,
                    ),
                )
                cursor.execute(self._function_statement())
                cursor.execute(
                    sql.SQL(
                        "CREATE TRIGGER {} BEFORE INSERT ON decision_events "
                        "FOR EACH ROW EXECUTE FUNCTION {}()"
                    ).format(
                        sql.Identifier(self.names.trigger),
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
        except Exception:
            try:
                self.cleanup()
            except Exception:
                pass
            raise

    @staticmethod
    def _request_scope(request: ApprovalRequest) -> tuple[str, int]:
        match = re.search(
            r"/api/projects/([^/]+)/chapters/([0-9]+)/review/approve$",
            request.url,
        )
        if match is None:
            raise RunnerError("approval request path is not canonical")
        return urllib.parse.unquote(match.group(1)), int(match.group(2))

    def _function_statement(self) -> sql.Composed:
        return sql.SQL(
            "CREATE FUNCTION {}() RETURNS trigger "
            "LANGUAGE plpgsql AS $forwin_minio_recovery$ "
            "DECLARE scoped_key bigint; waiter_name text; "
            "BEGIN "
            "SELECT advisory_key, waiter_application_name "
            "INTO scoped_key, waiter_name FROM {} "
            "WHERE project_id = NEW.project_id "
            "AND chapter_number = NEW.chapter_number "
            "AND reason = NEW.reason "
            "AND NEW.event_type = 'review_approved' "
            "AND NEW.actor_type = 'api'; "
            "IF scoped_key IS NOT NULL THEN "
            "PERFORM set_config('application_name', waiter_name, true); "
            "PERFORM pg_advisory_xact_lock(scoped_key); "
            "END IF; "
            "RETURN NEW; "
            "END "
            "$forwin_minio_recovery$"
        ).format(
            sql.Identifier(self.names.function),
            sql.Identifier(self.names.scope_table),
        )

    def observe_blocked_waiter(self) -> BarrierObservation:
        if self._admin is None or self._holder is None:
            raise RunnerError("review barrier is not installed")
        statement = """
            SELECT
                locks.pid,
                locks.granted,
                activity.application_name,
                activity.usename,
                activity.backend_type,
                activity.query,
                activity.wait_event_type,
                pg_blocking_pids(locks.pid) AS blocking_pids,
                (
                    SELECT count(*)::integer
                    FROM canon_commit_records AS commits
                    JOIN candidate_draft_records AS candidates
                      ON candidates.id = commits.candidate_id
                    JOIN {scope_table} AS scope
                      ON scope.project_id = commits.project_id
                     AND scope.chapter_number = commits.chapter_number
                    WHERE commits.status = 'committed'
                      AND candidates.status = 'accepted'
                ) AS canon_count
            FROM pg_locks AS locks
            JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
            WHERE locks.locktype = 'advisory'
              AND locks.classid::bigint = %s
              AND locks.objid::bigint = %s
            ORDER BY locks.granted DESC, locks.pid
        """.format(scope_table=self.names.scope_table)
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
            and "decision_events" in str(row.get("query") or "")
        ]
        if len(rows) != 2 or len(holders) != 1 or len(waiters) != 1:
            raise SetupBlocked(
                "review barrier requires exactly one holder and one waiter"
            )
        waiter = waiters[0]
        holder = holders[0]
        if (
            waiter.get("application_name")
            != self.waiter_application_name
            or waiter.get("usename") != self.database_role
            or waiter.get("backend_type") != "client backend"
        ):
            raise SetupBlocked(
                "review barrier waiter is not the bound approval API waiter"
            )
        holder_pid = int(holder["pid"])
        if [int(value) for value in waiter.get("blocking_pids") or []] != [
            holder_pid
        ]:
            raise SetupBlocked(
                "approval API waiter is not blocked only by the scoped holder"
            )
        canon_counts = {int(row.get("canon_count") or 0) for row in rows}
        if canon_counts != {1}:
            raise SetupBlocked(
                "review barrier did not observe exactly one committed Canon"
            )
        return BarrierObservation(
            holder_pid=holder_pid,
            waiter_pid=int(waiter["pid"]),
            holder_count=1,
            waiter_count=1,
            canon_count=1,
            waiter_application_name=self.waiter_application_name,
            database_role=self.database_role,
            backend_type="client backend",
        )

    def wait_for_blocked_waiter(
        self,
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.5,
    ) -> BarrierObservation:
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return self.observe_blocked_waiter()
            except SetupBlocked as exc:
                last_error = exc
                time.sleep(poll_seconds)
        raise SetupBlocked(
            "review_approved barrier did not produce the exact waiter: "
            f"{last_error or 'timeout'}"
        )

    def release(self) -> None:
        if self._released or self._holder is None:
            return
        with self._holder.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(%s) AS unlocked",
                (self.advisory_key,),
            )
            row = cursor.fetchone()
        if row is not None and row.get("unlocked") is False:
            raise RunnerError("review barrier advisory lock was not held")
        self._released = True

    def _object_residue_count(self) -> int:
        if self._admin is None:
            return 0
        with self._admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT (
                    (SELECT count(*) FROM pg_trigger
                     WHERE tgname = %s AND NOT tgisinternal)
                  + (SELECT count(*) FROM pg_proc WHERE proname = %s)
                  + (SELECT count(*) FROM pg_class WHERE relname = %s)
                )::integer AS residue_count
                /* task5 barrier object residue */
                """,
                (
                    self.names.trigger,
                    self.names.function,
                    self.names.scope_table,
                ),
            )
            row = cursor.fetchone() or {}
        return int(row.get("residue_count") or 0)

    def _lock_residue_count(self) -> int:
        if self._admin is None:
            return 0
        with self._admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)::integer AS residue_count
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid::bigint = %s
                  AND objid::bigint = %s
                /* task5 barrier lock residue */
                """,
                self.lock_identity,
            )
            row = cursor.fetchone() or {}
        return int(row.get("residue_count") or 0)

    def cleanup(self) -> None:
        errors: list[str] = []
        if self._holder is not None:
            try:
                self.release()
            except Exception as exc:
                errors.append(f"barrier release: {exc}")
            finally:
                try:
                    self._holder.close()
                except Exception as exc:
                    errors.append(f"holder close: {exc}")
                self._holder = None
        if self._admin is not None:
            try:
                with self._admin.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            "DROP TRIGGER IF EXISTS {} ON decision_events"
                        ).format(sql.Identifier(self.names.trigger))
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
                self.residue = {
                    "objects": self._object_residue_count(),
                    "locks": self._lock_residue_count(),
                }
                if any(self.residue.values()):
                    errors.append(f"review barrier residue: {self.residue}")
            except Exception as exc:
                errors.append(f"barrier object cleanup: {exc}")
            finally:
                try:
                    self._admin.close()
                except Exception as exc:
                    errors.append(f"admin close: {exc}")
                self._admin = None
        if errors:
            raise RunnerError("; ".join(errors))


class Database(Protocol):
    def fetch_all(
        self,
        statement: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]: ...

    def execute_conditional(
        self,
        statement: str,
        params: tuple[Any, ...],
    ) -> int: ...


class PsycopgDatabase:
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
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        connection = self.connect(self.database_url)
        try:
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(statement, params)
                return [dict(row) for row in cursor.fetchall()]
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def execute_conditional(
        self,
        statement: str,
        params: tuple[Any, ...],
    ) -> int:
        connection = self.connect(self.database_url)
        try:
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
                rowcount = int(cursor.rowcount)
            connection.commit()
            return rowcount
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


FIXTURE_SQL = """
    SELECT
        chapters.id AS chapter_id,
        candidates.id AS candidate_id,
        candidates.project_id,
        candidates.chapter_number,
        candidates.body_hash AS content_sha256,
        candidates.status AS candidate_status,
        candidates.idempotency_key AS candidate_idempotency_key,
        tasks.id AS task_id
    FROM chapter_plans AS chapters
    JOIN generation_tasks AS tasks
      ON tasks.project_id = chapters.project_id
    LEFT JOIN LATERAL (
        SELECT candidate.*
        FROM candidate_draft_records AS candidate
        WHERE candidate.project_id = chapters.project_id
          AND candidate.chapter_number = chapters.chapter_number
        ORDER BY candidate.version DESC, candidate.created_at DESC
        LIMIT 1
    ) AS candidates ON TRUE
    WHERE chapters.project_id = %s
      AND chapters.chapter_number = 1
      AND tasks.id = %s
    /* task5 fixture boundary */
"""

CANON_SQL = """
    SELECT
        commits.id AS canon_id,
        commits.idempotency_key AS natural_key,
        commits.project_id,
        candidates.chapter_plan_id AS chapter_id,
        commits.chapter_number,
        commits.candidate_id,
        candidates.version AS canon_version,
        candidates.body_hash AS content_sha256
    FROM canon_commit_records AS commits
    JOIN candidate_draft_records AS candidates
      ON candidates.id = commits.candidate_id
    WHERE commits.project_id = %s
      AND commits.chapter_number = %s
      AND commits.status = 'committed'
    ORDER BY commits.id
    /* task5 canon */
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
    /* task5 accepted bundle */
"""

MAINTENANCE_SQL = """
    SELECT
        id,
        idempotency_key AS natural_key,
        project_id,
        canon_commit_id AS canon_id,
        candidate_id,
        step_name,
        status,
        attempts AS attempt,
        lease_epoch,
        result_json,
        last_error
    FROM post_canon_maintenance_runs
    WHERE project_id = %s
      AND chapter_number = %s
      AND canon_commit_id = %s
    ORDER BY step_name, id
    /* task5 maintenance */
"""

PHASE3_OUTBOX_SQL = """
    SELECT
        id,
        event_id,
        aggregate_type,
        aggregate_id,
        event_type,
        payload_json,
        status,
        attempts,
        available_at,
        worker_id,
        lease_epoch,
        lease_expires_at,
        heartbeat_at,
        processed_at,
        error_message
    FROM outbox_events
    WHERE event_type = 'canon.phase3.requested'
      AND payload_json::jsonb ->> 'project_id' = %s
      AND (payload_json::jsonb ->> 'chapter_number')::integer = %s
    ORDER BY created_at, id
    /* task5 phase3 outbox */
"""

AUTHORITATIVE_SQL = """
    SELECT
        'canon'::text AS entity_type,
        commits.id AS record_id,
        commits.project_id,
        candidates.chapter_plan_id AS chapter_id,
        commits.idempotency_key AS natural_key
    FROM canon_commit_records AS commits
    JOIN candidate_draft_records AS candidates
      ON candidates.id = commits.candidate_id
    WHERE commits.project_id = %s
      AND commits.chapter_number = %s
      AND commits.status = 'committed'
    ORDER BY commits.id
    /* task5 authoritative */
"""

REPLAY_UPDATE_SQL = """
    UPDATE outbox_events
    SET status = 'pending',
        available_at = NULL,
        worker_id = '',
        lease_expires_at = NULL,
        heartbeat_at = NULL,
        processed_at = NULL,
        error_message = ''
    WHERE id = %s
      AND event_id = %s
      AND status = 'processed'
      AND attempts = %s
      AND available_at IS NOT DISTINCT FROM %s
      AND worker_id = %s
      AND lease_epoch = %s
      AND lease_expires_at IS NOT DISTINCT FROM %s
      AND heartbeat_at IS NOT DISTINCT FROM %s
      AND processed_at IS NOT DISTINCT FROM %s
      AND error_message = %s
      AND payload_json = %s
      AND aggregate_type = %s
      AND aggregate_id = %s
      AND event_type = 'canon.phase3.requested'
      AND payload_json::jsonb ->> 'canon_idempotency_key' = %s
    /* task5 exact same-event replay */
"""


@dataclass(frozen=True, slots=True)
class FixtureContext:
    fixture_id: str
    fault_id: str
    project_id: str
    chapter_number: int
    chapter_id: str
    task_id: str
    candidate_id: str
    canon_id: str = ""
    canon_natural_key: str = ""

    def evaluator_identity(self) -> dict[str, str]:
        return {
            "fixture_id": self.fixture_id,
            "fault_id": self.fault_id,
            "resource_type": "chapter",
            "resource_id": self.chapter_id,
        }


class SQLCollector:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._bound_endpoint_identity: dict[str, Any] | None = None

    def bind_endpoint_identity(
        self,
        endpoint_identity: Mapping[str, Any],
    ) -> None:
        if self._bound_endpoint_identity is not None:
            raise SetupBlocked("Task 5 endpoint identity was already bound")
        value = dict(endpoint_identity)
        if not value:
            raise SetupBlocked("Task 5 endpoint identity is empty")
        self._bound_endpoint_identity = value

    def _endpoint_identity(self) -> dict[str, Any]:
        if self._bound_endpoint_identity is None:
            raise SetupBlocked(
                "Task 5 endpoint identity was not bound before snapshot"
            )
        return dict(self._bound_endpoint_identity)

    def read_recovery_sentinel(self) -> dict[str, str]:
        rows = self.database.fetch_all(
            """
            SELECT sentinel_id, run_id, fault_id, source_sha
            FROM forwin_recovery_run_sentinel
            WHERE singleton = true
            """
        )
        if len(rows) != 1:
            raise SetupBlocked(
                "database endpoint returned no unique recovery sentinel"
            )
        return {
            "table": "forwin_recovery_run_sentinel",
            **{
                key: str(rows[0].get(key) or "")
                for key in ("sentinel_id", "run_id", "fault_id", "source_sha")
            },
        }

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
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            rows = self.database.fetch_all(
                FIXTURE_SQL,
                (project_id, task_id),
            )
            if len(rows) == 1:
                row = rows[0]
                if (
                    row.get("candidate_status") == "ready_for_canon"
                    and row.get("candidate_id")
                    and row.get("chapter_id")
                ):
                    return FixtureContext(
                        fixture_id=fixture_id,
                        fault_id=validate_fault_id(fault_id),
                        project_id=required_text(
                            row.get("project_id"),
                            "project_id",
                        ),
                        chapter_number=int(row.get("chapter_number") or 0),
                        chapter_id=required_text(
                            row.get("chapter_id"),
                            "chapter_id",
                        ),
                        task_id=required_text(row.get("task_id"), "task_id"),
                        candidate_id=required_text(
                            row.get("candidate_id"),
                            "candidate_id",
                        ),
                    )
            time.sleep(poll_seconds)
        raise SetupBlocked(
            "one-chapter fixture did not reach a review-ready candidate"
        )

    def _candidate(self, fixture: FixtureContext) -> dict[str, Any]:
        rows = self.database.fetch_all(
            FIXTURE_SQL,
            (fixture.project_id, fixture.task_id),
        )
        if len(rows) != 1:
            raise SetupBlocked("candidate fixture is missing or duplicated")
        row = rows[0]
        if (
            row.get("candidate_id") != fixture.candidate_id
            or row.get("chapter_id") != fixture.chapter_id
            or int(row.get("chapter_number") or 0) != fixture.chapter_number
        ):
            raise SetupBlocked("candidate fixture identity changed")
        return normalize_candidate(row)

    def _canon(self, fixture: FixtureContext) -> list[dict[str, Any]]:
        return normalize_canon_records(
            self.database.fetch_all(
                CANON_SQL,
                (fixture.project_id, fixture.chapter_number),
            )
        )

    def _accepted(self, fixture: FixtureContext) -> list[dict[str, Any]]:
        return normalize_accepted_bundles(
            self.database.fetch_all(
                ACCEPTED_SQL,
                (fixture.project_id, fixture.chapter_number),
            )
        )

    def _phase3_rows(self, fixture: FixtureContext) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            PHASE3_OUTBOX_SQL,
            (fixture.project_id, fixture.chapter_number),
        )

    def _maintenance_rows(
        self,
        fixture: FixtureContext,
    ) -> list[dict[str, Any]]:
        if not fixture.canon_id:
            return []
        return self.database.fetch_all(
            MAINTENANCE_SQL,
            (
                fixture.project_id,
                fixture.chapter_number,
                fixture.canon_id,
            ),
        )

    def pre_snapshot(
        self,
        *,
        source_sha: str,
        stage: str,
        fixture: FixtureContext,
    ) -> dict[str, Any]:
        snapshot = snapshot_envelope(
            source_sha=source_sha,
            fault_kind="minio_pre_canon_unavailable",
            fault_id=fixture.fault_id,
            stage=stage,
            fixture=fixture.evaluator_identity(),
            endpoint_identity=self._endpoint_identity(),
        )
        database = snapshot["state"]["database"]
        database["candidate"] = self._candidate(fixture)
        if stage in {"during", "after"}:
            database["canon_commits"] = self._canon(fixture)
        if stage == "after":
            database["authoritative_identities"] = normalize_authoritative(
                self.database.fetch_all(
                    AUTHORITATIVE_SQL,
                    (fixture.project_id, fixture.chapter_number),
                )
            )
        return snapshot

    def post_snapshot(
        self,
        *,
        source_sha: str,
        stage: str,
        fixture: FixtureContext,
        artifact: Mapping[str, Any] | None = None,
        replay_baseline_artifact: Mapping[str, Any] | None = None,
        phase3_replay_baseline: Mapping[str, Any] | None = None,
        phase3_replay_release: Mapping[str, Any] | None = None,
        phase3_replay_final: Mapping[str, Any] | None = None,
        barrier_residue_count: int = 0,
    ) -> dict[str, Any]:
        snapshot = snapshot_envelope(
            source_sha=source_sha,
            fault_kind="minio_post_canon_unavailable",
            fault_id=fixture.fault_id,
            stage=stage,
            fixture=fixture.evaluator_identity(),
            endpoint_identity=self._endpoint_identity(),
        )
        database = snapshot["state"]["database"]
        database["canon_commits"] = self._canon(fixture)
        database["accepted_bundles"] = self._accepted(fixture)
        if stage in {"during", "after"}:
            maintenance = [
                row
                for row in self._maintenance_rows(fixture)
                if row.get("step_name") == "world"
            ]
            if len(maintenance) != 1:
                raise SetupBlocked(
                    "fixture world maintenance identity is missing or duplicated"
                )
            database["maintenance"] = normalize_maintenance(maintenance[0])
        if stage == "after":
            if (
                artifact is None
                or replay_baseline_artifact is None
                or phase3_replay_baseline is None
                or phase3_replay_release is None
                or phase3_replay_final is None
            ):
                raise SetupBlocked(
                    "post-replay MinIO artifact or phase3 evidence is missing"
                )
            snapshot["state"]["external"]["replay_baseline_artifact"] = dict(
                replay_baseline_artifact
            )
            snapshot["state"]["external"]["artifact"] = dict(artifact)
            database["phase3_replay_baseline"] = copy.deepcopy(
                phase3_replay_baseline
            )
            database["phase3_replay_release"] = copy.deepcopy(
                phase3_replay_release
            )
            database["phase3_replay_final"] = copy.deepcopy(
                phase3_replay_final
            )
            database["authoritative_identities"] = normalize_authoritative(
                self.database.fetch_all(
                    AUTHORITATIVE_SQL,
                    (fixture.project_id, fixture.chapter_number),
                )
            )
            snapshot["state"]["barrier"]["residue_count"] = int(
                barrier_residue_count
            )
        return snapshot

    def require_pre_canon_failure(
        self,
        fixture: FixtureContext,
        *,
        expected_candidate: Mapping[str, Any],
    ) -> None:
        if self._candidate(fixture) != dict(expected_candidate):
            raise SetupBlocked("pre-Canon failed request changed candidate identity")
        if self._canon(fixture):
            raise SetupBlocked("pre-Canon failed request committed Canon")

    def wait_post_canon_boundary(
        self,
        fixture: FixtureContext,
        *,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 0.5,
    ) -> FixtureContext:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            canon = self._canon(fixture)
            accepted = self._accepted(fixture)
            phase3 = self._phase3_rows(fixture)
            if len(canon) == len(accepted) == len(phase3) == 1:
                try:
                    identity = self._event_identity(phase3[0])
                except SetupBlocked:
                    identity = {}
                payload = identity.get("payload") or {}
                if (
                    phase3[0].get("status") == "pending"
                    and identity.get("event_id")
                    == f"{canon[0]['natural_key']}:{PHASE3_EVENT_TYPE}"
                    and identity.get("event_type") == PHASE3_EVENT_TYPE
                    and identity.get("aggregate_type") == "project"
                    and identity.get("aggregate_id") == fixture.project_id
                    and payload.get("canon_commit_id") == canon[0]["canon_id"]
                    and payload.get("canon_idempotency_key")
                    == canon[0]["natural_key"]
                    and payload.get("project_id") == fixture.project_id
                    and int(payload.get("chapter_number") or 0)
                    == fixture.chapter_number
                    and payload.get("candidate_id") == fixture.candidate_id
                ):
                    return replace(
                        fixture,
                        canon_id=canon[0]["canon_id"],
                        canon_natural_key=canon[0]["natural_key"],
                    )
            time.sleep(poll_seconds)
        raise SetupBlocked(
            "post-Canon barrier did not expose one Canon and one pending phase3 event"
        )

    def require_maintenance_pending(self, fixture: FixtureContext) -> None:
        rows = self._maintenance_rows(fixture)
        world = [row for row in rows if row.get("step_name") == "world"]
        if len(world) != 1 or world[0].get("status") not in {
            "pending",
            "failed",
        }:
            raise SetupBlocked(
                "MinIO outage did not leave world maintenance retryable"
            )
        phase3 = self._phase3_rows(fixture)
        if len(phase3) != 1 or phase3[0].get("status") != "pending":
            raise SetupBlocked(
                "held phase3 event is not pending after maintenance failure"
            )

    def wait_converged(
        self,
        fixture: FixtureContext,
        inventory: MinioInventory,
        *,
        timeout_seconds: float = 600.0,
        poll_seconds: float = 1.0,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                canon = self._canon(fixture)
                accepted = self._accepted(fixture)
                phase3 = self._phase3_rows(fixture)
                maintenance = self._maintenance_rows(fixture)
                steps = {str(row.get("step_name") or "") for row in maintenance}
                if (
                    len(canon) == 1
                    and len(accepted) == 1
                    and len(phase3) == 1
                    and phase3[0].get("status") == "processed"
                    and steps == set(POST_CANON_STEPS)
                    and len(maintenance) == len(POST_CANON_STEPS)
                    and all(
                        row.get("status") == "succeeded"
                        for row in maintenance
                    )
                ):
                    return inventory.require_world_object(
                        project_id=fixture.project_id,
                        canon_id=fixture.canon_id or canon[0]["canon_id"],
                    )
            except Exception as exc:
                last_error = exc
            time.sleep(poll_seconds)
        raise SetupBlocked(
            "Canon/outbox/maintenance/MinIO did not converge: "
            f"{last_error or 'timeout'}"
        )

    @staticmethod
    def _event_identity(row: Mapping[str, Any]) -> dict[str, Any]:
        payload = _json_payload(row.get("payload_json"), "phase3 payload")
        expected_keys = {
            "schema_version",
            "canon_commit_id",
            "canon_idempotency_key",
            "project_id",
            "chapter_number",
            "candidate_id",
        }
        if set(payload) != expected_keys:
            raise SetupBlocked("phase3 payload schema is not exact")
        return {
            "row_id": required_text(row.get("id"), "outbox row id"),
            "event_id": required_text(row.get("event_id"), "event_id"),
            "aggregate_type": required_text(
                row.get("aggregate_type"),
                "aggregate_type",
            ),
            "aggregate_id": required_text(
                row.get("aggregate_id"),
                "aggregate_id",
            ),
            "event_type": required_text(row.get("event_type"), "event_type"),
            "payload": payload,
            "payload_sha256": stable_hash(payload),
            "canon_idempotency_key": required_text(
                payload.get("canon_idempotency_key"),
                "canon_idempotency_key",
            ),
        }

    @classmethod
    def _phase3_observation(
        cls,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            **cls._event_identity(row),
            "status": required_text(row.get("status"), "phase3 status"),
            "attempts": _required_counter(
                row.get("attempts"),
                "phase3 attempts",
            ),
            "lease_epoch": _required_counter(
                row.get("lease_epoch"),
                "phase3 lease_epoch",
            ),
        }

    def phase3_replay_observation(
        self,
        fixture: FixtureContext,
    ) -> dict[str, Any]:
        rows = self._phase3_rows(fixture)
        if len(rows) != 1:
            raise RunnerError(
                "same-event replay requires exactly one phase3 row"
            )
        return self._phase3_observation(rows[0])

    def release_processed_phase3_event(
        self,
        fixture: FixtureContext,
    ) -> dict[str, Any]:
        before_rows = self._phase3_rows(fixture)
        if len(before_rows) != 1:
            raise RunnerError("same-event replay requires exactly one phase3 row")
        before = before_rows[0]
        if before.get("status") != "processed":
            raise RunnerError("same-event replay phase3 row is not processed")
        baseline = self._phase3_observation(before)
        if (
            baseline["event_type"] != PHASE3_EVENT_TYPE
            or baseline["aggregate_type"] != "project"
            or baseline["aggregate_id"] != fixture.project_id
            or baseline["payload"]["canon_commit_id"] != fixture.canon_id
            or baseline["canon_idempotency_key"]
            != fixture.canon_natural_key
        ):
            raise RunnerError("same-event replay identity is not fixture-bound")
        raw_payload = before.get("payload_json")
        payload_text = (
            raw_payload
            if isinstance(raw_payload, str)
            else json.dumps(raw_payload, ensure_ascii=False, sort_keys=True)
        )
        params = (
            before.get("id"),
            before.get("event_id"),
            int(before.get("attempts") or 0),
            before.get("available_at"),
            before.get("worker_id"),
            int(before.get("lease_epoch") or 0),
            before.get("lease_expires_at"),
            before.get("heartbeat_at"),
            before.get("processed_at"),
            str(before.get("error_message") or ""),
            payload_text,
            before.get("aggregate_type"),
            before.get("aggregate_id"),
            baseline["canon_idempotency_key"],
        )
        rowcount = self.database.execute_conditional(
            REPLAY_UPDATE_SQL,
            params,
        )
        if rowcount != 1:
            raise RunnerError(
                f"same-event replay conditional update rowcount is {rowcount}"
            )
        after_rows = self._phase3_rows(fixture)
        if len(after_rows) != 1:
            raise RunnerError("same-event replay changed phase3 row cardinality")
        after = after_rows[0]
        release = self._phase3_observation(after)
        identity_fields = (
            "row_id",
            "event_id",
            "aggregate_type",
            "aggregate_id",
            "event_type",
            "payload",
            "payload_sha256",
            "canon_idempotency_key",
        )
        if any(release[field] != baseline[field] for field in identity_fields):
            raise RunnerError("same-event replay identity changed")
        if release["status"] != "pending":
            raise RunnerError("same-event replay row was not released to pending")
        if (
            release["attempts"] != baseline["attempts"]
            or release["lease_epoch"] != baseline["lease_epoch"]
        ):
            raise RunnerError(
                "same-event replay conditional mutation claim counters changed"
            )
        before_safe = _json_safe(before)
        after_safe = _json_safe(after)
        return {
            "schema_version": 1,
            "fault_id": fixture.fault_id,
            "fixture": fixture.evaluator_identity(),
            "baseline": baseline,
            "release": {
                **release,
                "conditional_rowcount": rowcount,
                "predicate_sha256": stable_hash(
                    {
                        "statement": "task5 exact same-event replay",
                        "params": _json_safe(params),
                    }
                ),
                "before_row_sha256": stable_hash(before_safe),
                "after_row_sha256": stable_hash(after_safe),
            },
        }

    def identity_world(
        self,
        fixture: FixtureContext,
        object_inventory: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        canon = self._canon(fixture)
        accepted = self._accepted(fixture)
        phase3 = self._phase3_rows(fixture)
        maintenance = self._maintenance_rows(fixture)
        if (
            len(canon) != 1
            or len(accepted) != 1
            or len(phase3) != 1
            or len(maintenance) != len(POST_CANON_STEPS)
        ):
            raise SetupBlocked("replay world identity cardinality is incomplete")
        maintenance_identities = [
            {
                "id": required_text(row.get("id"), "maintenance id"),
                "natural_key": required_text(
                    row.get("natural_key"),
                    "maintenance natural_key",
                ),
                "canon_id": required_text(row.get("canon_id"), "canon_id"),
                "candidate_id": required_text(
                    row.get("candidate_id"),
                    "candidate_id",
                ),
                "step_name": required_text(
                    row.get("step_name"),
                    "step_name",
                ),
            }
            for row in maintenance
        ]
        natural_keys = [row["natural_key"] for row in maintenance_identities]
        if any(count != 1 for count in Counter(natural_keys).values()):
            raise SetupBlocked("maintenance natural keys are duplicated")
        objects = [dict(row) for row in object_inventory]
        object_keys = [str(row.get("key") or "") for row in objects]
        if any(count != 1 for count in Counter(object_keys).values()):
            raise SetupBlocked("MinIO object identities are duplicated")
        return {
            "canon": canon,
            "accepted_bundles": accepted,
            "phase3_event": self._event_identity(phase3[0]),
            "maintenance": maintenance_identities,
            "objects": objects,
        }


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
        sql_collector: SQLCollector,
        api: ApprovalAPI,
        inventory: MinioInventory,
        writer: EvidenceWriter,
        barrier_factory: Callable[[], ReviewApprovedBarrier],
        api_url: str,
        mcp_url: str,
        database_url: str,
        minio_url: str,
    ) -> None:
        if fault_kind not in SUPPORTED_FAULTS:
            raise RunnerError(f"unsupported Task 5 fault: {fault_kind}")
        self.fault_kind = fault_kind
        self.fault_id = validate_fault_id(fault_id)
        self.source_sha = source_sha
        self.controller = controller
        self.lifecycle = lifecycle
        self.sql = sql_collector
        self.api = api
        self.inventory = inventory
        self.writer = writer
        self.barrier_factory = barrier_factory
        self._configured_api_url = api_url
        self.mcp_url = mcp_url
        self.database_url = database_url
        self.minio_url = minio_url
        self.stage = "initial"
        self.stack_started = False
        self.terminal = False
        self.barrier: ReviewApprovedBarrier | None = None
        self.async_approval: AsyncApproval | None = None
        self.supplemental: dict[str, dict[str, Any]] = {}

    def run(self) -> LiveRunResult:
        snapshots: dict[str, dict[str, Any]] | None = None
        failure: BaseException | None = None
        cleanup_errors: list[str] = []
        try:
            self.stage = "fresh_up"
            self.stack_started = True
            self.controller.fresh_up(self.fault_id)
            self.stage = "bind_endpoints"
            common.require_client_endpoint(
                self.lifecycle,
                attribute="mcp_url",
                expected_url=self.mcp_url,
                label="MCP",
            )
            common.require_client_endpoint(
                self.api,
                attribute="api_url",
                expected_url=self._configured_api_url,
                label="API",
            )
            common.require_client_endpoint(
                self.inventory,
                attribute="endpoint_url",
                expected_url=self.minio_url,
                label="MinIO",
            )
            endpoint_identity = common.bind_recovery_endpoints(
                controller=self.controller,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                api_url=self._configured_api_url,
                mcp_url=self.mcp_url,
                database_url=self.database_url,
                minio_url=self.minio_url,
                sentinel_reader=self.sql.read_recovery_sentinel,
            )
            self.sql.bind_endpoint_identity(endpoint_identity)
            snapshots = (
                self._run_pre_canon()
                if self.fault_kind == "minio_pre_canon_unavailable"
                else self._run_post_canon()
            )
            self.stage = "destroy"
            self.controller.destroy()
            self.terminal = True
        except BaseException as exc:
            failure = exc
        finally:
            try:
                cleanup_errors.extend(self._cleanup_local_resources())
            except BaseException as exc:
                failure = exc
            try:
                self.inventory.close()
            except BaseException as exc:
                cleanup_errors.append(f"MinIO client close: {exc}")
                if not isinstance(exc, Exception):
                    failure = exc

        if failure is not None and not isinstance(failure, Exception):
            if self.stack_started and not self.terminal:
                try:
                    self.controller.interrupt_cleanup(self.fault_id)
                    self.terminal = True
                except BaseException as exc:
                    cleanup_errors.append(f"interrupt cleanup: {exc}")
            raise failure.with_traceback(failure.__traceback__)
        if failure is None and cleanup_errors:
            failure = RunnerError("; ".join(cleanup_errors))
            self.stage = "cleanup"
        if failure is not None:
            if self.stack_started and not self.terminal:
                try:
                    self.controller.abort(
                        self.fault_id,
                        self._abort_stage(),
                        self._failure_reason(failure),
                    )
                    self.terminal = True
                except BaseException as exc:
                    cleanup_errors.append(f"controller abort: {exc}")
            report = self.writer.write_setup_blocked(
                fault_kind=self.fault_kind,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                failure_stage=self.stage,
                failure_reason=self._failure_reason(failure),
                cleanup_errors=cleanup_errors,
            )
            return LiveRunResult(status="setup_blocked", report_path=report)
        if snapshots is None:
            raise RunnerError("Task 5 runner produced no snapshots")
        self.stage = "evidence_write"
        report = self.writer.write_pass_report(
            fault_kind=self.fault_kind,
            fault_id=self.fault_id,
            source_sha=self.source_sha,
            snapshots=snapshots,
            event_log_path=self.controller.event_log_path,
            supplemental_artifacts=self.supplemental,
        )
        return LiveRunResult(status="pass", report_path=report)

    def _fixture(self) -> FixtureContext:
        self.stage = "genesis"
        project = asyncio.run(self.lifecycle.create_genesis_project())
        self.stage = "writing_handoff"
        task = asyncio.run(self.lifecycle.start_writing(project.project_id))
        self.stage = "review_ready"
        return self.sql.wait_review_ready(
            fault_id=self.fault_id,
            fixture_id=f"fixture-{self.fault_id}",
            project_id=project.project_id,
            task_id=task.task_id,
        )

    def _request(self, fixture: FixtureContext) -> ApprovalRequest:
        return ApprovalRequest.for_fixture(
            api_url=self.api_url,
            project_id=fixture.project_id,
            chapter_number=fixture.chapter_number,
            fault_id=self.fault_id,
            candidate_id=fixture.candidate_id,
        )

    @property
    def api_url(self) -> str:
        value = getattr(self.api, "api_url", "")
        if value:
            return str(value)
        transport_url = getattr(self.api, "base_url", "")
        if transport_url:
            return str(transport_url)
        configured = getattr(self, "_configured_api_url", "")
        if configured:
            return str(configured)
        raise RunnerError("Approval API URL is unavailable")

    def _run_pre_canon(self) -> dict[str, dict[str, Any]]:
        fixture = self._fixture()
        request = self._request(fixture)
        before = self.sql.pre_snapshot(
            source_sha=self.source_sha,
            stage="before",
            fixture=fixture,
        )
        candidate_before = copy.deepcopy(
            before["state"]["database"]["candidate"]
        )
        self.stage = "minio_stop"
        self.controller.stop(MINIO_SERVICE, self.fault_id)
        self.stage = "approval_failure"
        failure_detail = ""
        try:
            self.api.send(request)
        except ApprovalTransportFailure as exc:
            failure_detail = str(exc)
        if not failure_detail:
            raise SetupBlocked(
                "pre-Canon MinIO outage did not fail the approval request"
            )
        self.sql.require_pre_canon_failure(
            fixture,
            expected_candidate=candidate_before,
        )
        during = self.sql.pre_snapshot(
            source_sha=self.source_sha,
            stage="during",
            fixture=fixture,
        )
        self.stage = "minio_start"
        self.controller.start(MINIO_SERVICE, self.fault_id)
        self.stage = "identical_approval_replay"
        response = self.api.send(request)
        self.stage = "convergence"
        canon = self.sql._canon(fixture)
        deadline = time.monotonic() + 300.0
        while len(canon) != 1 and time.monotonic() < deadline:
            time.sleep(0.5)
            canon = self.sql._canon(fixture)
        if len(canon) != 1:
            raise SetupBlocked("pre-Canon approval replay did not commit one Canon")
        fixture = replace(
            fixture,
            canon_id=canon[0]["canon_id"],
            canon_natural_key=canon[0]["natural_key"],
        )
        artifact, objects = self.sql.wait_converged(fixture, self.inventory)
        after = self.sql.pre_snapshot(
            source_sha=self.source_sha,
            stage="after",
            fixture=fixture,
        )
        self.supplemental["request-replay.json"] = {
            "schema_version": 1,
            "fault_kind": self.fault_kind,
            "fault_id": self.fault_id,
            "request_identity_sha256": request.identity_sha256,
            "candidate_id": request.candidate_id,
            "first_attempt": {
                "outcome": "failed_before_canon",
                "error_sha256": stable_hash(failure_detail),
            },
            "replay": {
                "outcome": str(response.get("status") or ""),
                "request_identity_sha256": request.identity_sha256,
            },
            "convergence": {
                "canon_id": fixture.canon_id,
                "artifact": artifact,
                "object_inventory_sha256": stable_hash(objects),
            },
        }
        return {"before": before, "during": during, "after": after}

    def _run_post_canon(self) -> dict[str, dict[str, Any]]:
        fixture = self._fixture()
        request = self._request(fixture)
        self.stage = "outbox_preapproval_hold"
        self.controller.setup_hold(
            OUTBOX_SERVICE,
            self.fault_id,
            PRE_APPROVAL_HOLD,
            fault_kind=self.fault_kind,
            purpose="auxiliary",
        )
        self.stage = "review_barrier_install"
        self.barrier = self.barrier_factory()
        self.barrier.install(request=request)
        self.stage = "async_approval"
        self.async_approval = AsyncApproval(self.api, request)
        self.async_approval.start()
        self.stage = "review_barrier_wait"
        observation = self.barrier.wait_for_blocked_waiter()
        self.async_approval.require_blocked()
        fixture = self.sql.wait_post_canon_boundary(fixture)
        before = self.sql.post_snapshot(
            source_sha=self.source_sha,
            stage="before",
            fixture=fixture,
        )
        self.stage = "minio_stop"
        self.controller.stop(MINIO_SERVICE, self.fault_id)
        self.stage = "review_barrier_release"
        self.barrier.release()
        response = self.async_approval.join()
        self.async_approval = None
        if response.get("status") != "maintenance_pending":
            raise SetupBlocked(
                "post-Canon MinIO outage did not return maintenance_pending"
            )
        self.sql.require_maintenance_pending(fixture)
        during = self.sql.post_snapshot(
            source_sha=self.source_sha,
            stage="during",
            fixture=fixture,
        )
        self.stage = "review_barrier_cleanup"
        self.barrier.cleanup()
        residue = dict(self.barrier.residue or {})
        if residue != {"objects": 0, "locks": 0}:
            raise RunnerError(f"review barrier residue is not zero: {residue}")
        self.barrier = None
        self.stage = "minio_start"
        self.controller.start(MINIO_SERVICE, self.fault_id)
        self.stage = "outbox_preapproval_release"
        self.controller.setup_release(
            OUTBOX_SERVICE,
            self.fault_id,
            PRE_APPROVAL_HOLD,
        )
        self.stage = "initial_convergence"
        baseline_artifact, baseline_objects = self.sql.wait_converged(
            fixture,
            self.inventory,
        )
        baseline_world = self.sql.identity_world(fixture, baseline_objects)
        self.stage = "outbox_replay_hold"
        self.controller.setup_hold(
            OUTBOX_SERVICE,
            self.fault_id,
            SAME_EVENT_REPLAY_HOLD,
            fault_kind=self.fault_kind,
            purpose="auxiliary",
        )
        self.stage = "same_event_release"
        replay = self.sql.release_processed_phase3_event(fixture)
        self.stage = "outbox_replay_release"
        self.controller.setup_release(
            OUTBOX_SERVICE,
            self.fault_id,
            SAME_EVENT_REPLAY_HOLD,
        )
        self.stage = "replay_convergence"
        final_artifact, final_objects = self.sql.wait_converged(
            fixture,
            self.inventory,
        )
        replay_final = self.sql.phase3_replay_observation(fixture)
        final_world = self.sql.identity_world(fixture, final_objects)
        if final_world != baseline_world:
            raise RunnerError(
                "same-event replay created identity, duplicate, or object drift"
            )
        after = self.sql.post_snapshot(
            source_sha=self.source_sha,
            stage="after",
            fixture=fixture,
            replay_baseline_artifact=baseline_artifact,
            artifact=final_artifact,
            phase3_replay_baseline=replay["baseline"],
            phase3_replay_release=replay["release"],
            phase3_replay_final=replay_final,
            barrier_residue_count=sum(residue.values()),
        )
        self.supplemental["barrier-observation.json"] = {
            "schema_version": 1,
            "fault_kind": self.fault_kind,
            "fault_id": self.fault_id,
            "holder_pid": observation.holder_pid,
            "waiter_pid": observation.waiter_pid,
            "holder_count": observation.holder_count,
            "waiter_count": observation.waiter_count,
            "canon_count": observation.canon_count,
            "waiter_application_name": observation.waiter_application_name,
            "database_role": observation.database_role,
            "backend_type": observation.backend_type,
            "request_identity_sha256": request.identity_sha256,
            "sql_objects": {
                "scope_table": barrier_names(self.fault_id).scope_table,
                "function": barrier_names(self.fault_id).function,
                "trigger": barrier_names(self.fault_id).trigger,
            },
            "residue": residue,
        }
        self.supplemental["same-event-replay.json"] = {
            **replay,
            "final": replay_final,
            "baseline_world_sha256": stable_hash(baseline_world),
            "final_world_sha256": stable_hash(final_world),
            "maintenance_identity_unchanged": (
                baseline_world["maintenance"] == final_world["maintenance"]
            ),
            "artifact_identity_unchanged": (
                baseline_artifact == final_artifact
            ),
            "no_new_identity_or_residue": True,
        }
        self.supplemental["minio-inventory.json"] = {
            "schema_version": 1,
            "expected_world_key": expected_world_object_key(
                project_id=fixture.project_id,
                canon_id=fixture.canon_id,
                prefix=self.inventory.prefix,
            ),
            "baseline": baseline_objects,
            "after_replay": final_objects,
            "identity_unchanged": baseline_objects == final_objects,
        }
        return {"before": before, "during": during, "after": after}

    def _cleanup_local_resources(self) -> list[str]:
        errors: list[str] = []
        interruption: BaseException | None = None
        if self.barrier is not None:
            try:
                self.barrier.release()
            except BaseException as exc:
                errors.append(f"barrier release: {exc}")
                if not isinstance(exc, Exception) and interruption is None:
                    interruption = exc
        if self.barrier is not None:
            try:
                self.barrier.cleanup()
            except BaseException as exc:
                errors.append(f"barrier cleanup: {exc}")
                if not isinstance(exc, Exception) and interruption is None:
                    interruption = exc
            self.barrier = None
        if self.async_approval is not None:
            try:
                self.async_approval.join(timeout_seconds=120.0)
            except BaseException as exc:
                errors.append(f"approval thread join: {exc}")
                if not isinstance(exc, Exception) and interruption is None:
                    interruption = exc
            self.async_approval = None
        if interruption is not None:
            raise interruption.with_traceback(interruption.__traceback__)
        return errors

    def _abort_stage(self) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", self.stage).strip("-")
        return (normalized or "task5")[:128]

    @staticmethod
    def _failure_reason(error: BaseException) -> str:
        detail = " ".join(
            (str(error) or type(error).__name__).replace("\x00", " ").split()
        )
        return detail[:512] or "Task5 setup blocked"


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
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_prefix: str
    minio_secure: bool


def _required_environment(
    environ: Mapping[str, str],
    name: str,
) -> str:
    value = str(environ.get(name) or "").strip()
    if not value:
        raise RunnerError(f"required environment is empty: {name}")
    return value


def resolve_run_config(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] = os.environ,
) -> RunConfig:
    fault_kind = str(args.fault_kind or "")
    if fault_kind not in SUPPORTED_FAULTS:
        raise RunnerError(f"unsupported Task 5 fault: {fault_kind}")
    fault_id = validate_fault_id(args.fault_id)
    identity = candidate_identity(Path(args.candidate_manifest))
    database_env = str(args.database_url_env or "")
    if ENV_NAME_PATTERN.fullmatch(database_env) is None:
        raise RunnerError("database URL environment name is invalid")
    database_url = normalize_database_url(
        _required_environment(environ, database_env)
    )
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise RunnerError("database URL must use PostgreSQL")
    evidence_dir = Path(args.evidence_dir).expanduser().resolve()
    if evidence_dir.exists() and any(evidence_dir.iterdir()):
        raise RunnerError(
            f"evidence directory is not empty; use a new directory: {evidence_dir}"
        )
    secure_value = str(
        environ.get("FORWIN_RECOVERY_MINIO_SECURE") or "false"
    ).strip().lower()
    if secure_value not in {"true", "false"}:
        raise RunnerError(
            "FORWIN_RECOVERY_MINIO_SECURE must be true or false"
        )
    if secure_value == "true":
        raise RunnerError(
            "recovery MinIO endpoint binding requires loopback HTTP"
        )
    minio_endpoint = _required_environment(
        environ,
        "FORWIN_RECOVERY_MINIO_ENDPOINT",
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
        minio_endpoint=minio_endpoint,
        minio_access_key=_required_environment(
            environ,
            "FORWIN_RECOVERY_MINIO_ACCESS_KEY",
        ),
        minio_secret_key=_required_environment(
            environ,
            "FORWIN_RECOVERY_MINIO_SECRET_KEY",
        ),
        minio_bucket=_required_environment(
            environ,
            "FORWIN_RECOVERY_MINIO_BUCKET",
        ),
        minio_prefix=_required_environment(
            environ,
            "FORWIN_RECOVERY_MINIO_PREFIX",
        ).strip("/"),
        minio_secure=secure_value == "true",
    )


def build_live_runner(config: RunConfig) -> LiveRunner:
    from minio import Minio

    controller = RecoveryController(
        candidate_manifest=config.candidate_manifest,
        evidence_dir=config.evidence_dir,
    )
    lifecycle = OneChapterLifecycle(
        mcp_url=config.mcp_url,
        fault_id=config.fault_id,
    )
    api = ApprovalAPI(api_url=config.api_url)
    inventory = MinioInventory(
        client=Minio(
            config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            secure=config.minio_secure,
        ),
        bucket=config.minio_bucket,
        prefix=config.minio_prefix,
        endpoint_url=f"http://{config.minio_endpoint}",
    )
    return LiveRunner(
        fault_kind=config.fault_kind,
        fault_id=config.fault_id,
        source_sha=config.source_sha,
        controller=controller,
        lifecycle=lifecycle,
        sql_collector=SQLCollector(PsycopgDatabase(config.database_url)),
        api=api,
        inventory=inventory,
        writer=EvidenceWriter(evidence_dir=config.evidence_dir),
        api_url=config.api_url,
        mcp_url=config.mcp_url,
        database_url=config.database_url,
        minio_url=f"http://{config.minio_endpoint}",
        barrier_factory=lambda: ReviewApprovedBarrier(
            fault_id=config.fault_id,
            database_url=config.database_url,
        ),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run fault-local MinIO recovery proofs."
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
