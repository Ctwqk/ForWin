#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

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
GENERATION_TASK_TERMINAL_STATUSES = frozenset(
    {
        "cancelled",
        "completed",
        "failed",
        "needs_review",
        "partial_failed",
        "paused",
        "succeeded",
    }
)
GENERATION_BOUNDARY_TIMEOUT_SECONDS = 900.0
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
QDRANT_PROJECTION_TYPES = ("chapter_memory", "llm_kb")
QDRANT_ORACLE_ARTIFACT_NAME = "qdrant-oracle-manifest.json"
QDRANT_ORACLE_PHASES = ("replay_baseline", "final")
LLM_KB_PROJECTION_VERSION = "llm_kb_v2"
LLM_KB_ROOT_FILE_KEYS = frozenset(
    {
        "CURRENT_STATE.md",
        "NEXT_CHAPTER_CONTEXT.md",
        "ACTIVE_THREADS.md",
        "CHARACTER_MEMORY.md",
        "FACTION_MEMORY.md",
        "MAP_CONTEXT.md",
        "READER_PROMISES.md",
        "KNOWLEDGE_GAPS.md",
        "REVEAL_LADDER.md",
        "MUST_NOT_REVEAL.md",
        "RECENT_CHANGES.md",
        "STYLE_AND_TONE.md",
        "CONSTRAINTS.md",
        "facts.jsonl",
        "events.jsonl",
        "graph_deltas.jsonl",
        "open_questions.jsonl",
    }
)
LLM_KB_VECTOR_ROLES = ("reviewer", "planner", "compiler")
QDRANT_AMBIENT_COLLECTION_OVERRIDES = (
    "FORWIN_RECOVERY_QDRANT_COLLECTION",
    "FORWIN_RECOVERY_CHAPTER_MEMORY_QDRANT_COLLECTION",
    "FORWIN_RECOVERY_LLM_KB_QDRANT_COLLECTION",
    "FORWIN_QDRANT_COLLECTION",
    "FORWIN_LLM_KB_QDRANT_COLLECTION",
)


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
        timeout_seconds: float = GENERATION_BOUNDARY_TIMEOUT_SECONDS,
        poll_seconds: float = 0.5,
        stop_reason: Callable[[], str] | None = None,
    ) -> BarrierObservation:
        deadline = time.monotonic() + timeout_seconds
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                return self.observe_blocked_waiter()
            except SetupBlocked as exc:
                last_error = exc
                reason = str(stop_reason() if stop_reason is not None else "").strip()
                if reason:
                    raise SetupBlocked(reason) from exc
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


def _oracle_point_id(*parts: object) -> str:
    # SHA-1 mirrors the stable production identity contract; it is not security.
    digest = hashlib.sha1(
        ":".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:32]
    return str(UUID(digest))


def build_expected_chapter_memory_points(
    rows: Sequence[Mapping[str, Any]],
    *,
    project_id: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    if not rows:
        raise SetupBlocked("Canon chapter memory oracle is missing")
    points: list[dict[str, Any]] = []
    seen_chapters: set[int] = set()
    for row in rows:
        raw_chapter = row.get("chapter_number") if isinstance(row, Mapping) else None
        if (
            not isinstance(row, Mapping)
            or row.get("project_id") != project_id
            or type(raw_chapter) is not int
            or not 1 <= int(raw_chapter) <= int(chapter_number)
            or int(raw_chapter) in seen_chapters
        ):
            raise SetupBlocked(
                "Canon chapter memory oracle is not uniquely target-bound"
            )
        normalized_chapter = int(raw_chapter)
        seen_chapters.add(normalized_chapter)
        title = str(row.get("title") or "")
        summary = str(row.get("summary") or "")
        body = str(row.get("body_text") or "")
        points.append(
            {
                "id": _oracle_point_id(project_id, normalized_chapter),
                "payload": {
                    "project_id": project_id,
                    "chapter_number": normalized_chapter,
                    "title": title,
                    "summary": summary,
                    "excerpt": body[:500],
                },
            }
        )
    return sorted(points, key=lambda point: int(point["payload"]["chapter_number"]))


def _oracle_trim(text: object, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _oracle_role_scope(file_key: str) -> str:
    if "review" in file_key or "risk" in file_key:
        return "reviewer"
    if "plan" in file_key or "outline" in file_key:
        return "planner"
    return "writer"


def _oracle_visibility(role_scope: str) -> str:
    return {
        "reviewer": "reviewer_only",
        "planner": "planner_only",
        "compiler": "compiler_only",
    }.get(role_scope, "writer_safe")


def _oracle_reference_fields(
    file_key: str,
    text: str,
    source_refs: list[str],
    *,
    raw_payload: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    blob = "\n".join([text, *source_refs])

    def ref_ids(ref_type: str) -> set[str]:
        return set(
            re.findall(
                rf"(?:book_state:{ref_type}:|{ref_type}:)"
                r"([A-Za-z0-9_.-]+)",
                blob,
            )
        )

    node_refs = ref_ids("node")
    edge_refs = ref_ids("edge")
    fact_refs = ref_ids("fact")
    map_refs = {
        f"{left or right}:{item_id}"
        for left, right, item_id in re.findall(
            r"(?:book_state:(map_node|map_edge):|(map_node|map_edge):)"
            r"([A-Za-z0-9_.-]+)",
            blob,
        )
    }
    chapter_refs = set(re.findall(r"\bchapter:\d+\b", blob))
    if raw_payload is not None:
        item_id = str(raw_payload.get("id") or "").strip()
        if item_id:
            if file_key == "facts.jsonl":
                fact_refs.add(item_id)
            elif file_key == "graph_deltas.jsonl":
                target_type = str(raw_payload.get("target_type") or "").strip()
                target_id = str(raw_payload.get("target_id") or "").strip()
                if target_type == "node" and target_id:
                    node_refs.add(target_id)
                elif target_type == "edge" and target_id:
                    edge_refs.add(target_id)
            else:
                node_refs.add(item_id)
        raw_chapter = raw_payload.get("as_of_chapter") or raw_payload.get(
            "chapter_number"
        )
        if raw_chapter:
            chapter_refs.add(f"chapter:{int(raw_chapter)}")
    return {
        "node_refs": sorted(node_refs),
        "edge_refs": sorted(edge_refs),
        "fact_refs": sorted(fact_refs),
        "map_refs": sorted(map_refs),
        "chapter_refs": sorted(chapter_refs),
    }


def _oracle_section(
    *,
    file_key: str,
    section_key: str,
    role_scope: str,
    text: str,
    source_refs: list[str],
    source_digest: str,
    chapter_number: int,
    raw_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    refs = _oracle_reference_fields(
        file_key,
        text,
        source_refs,
        raw_payload=raw_payload,
    )
    visibility_scope = _oracle_visibility(role_scope)
    section_digest = hashlib.sha1(
        json.dumps(
            {
                "file_key": file_key,
                "section_key": section_key,
                "role_scope": role_scope,
                "visibility_scope": visibility_scope,
                "text": text,
                "source_refs": source_refs,
                **refs,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "index_kind": "llm_kb",
        "as_of_chapter": int(chapter_number),
        "projection_version": LLM_KB_PROJECTION_VERSION,
        "file_key": file_key,
        "section_key": section_key,
        "role_scope": role_scope,
        "visibility_scope": visibility_scope,
        "canon_status": "canon_projection",
        **refs,
        "text": text,
        "source_refs": source_refs,
        "source_digest": source_digest,
        "section_digest": section_digest,
    }


def _oracle_markdown_sections(
    file_key: str,
    content: str,
    *,
    source_digest: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    chunks: list[tuple[str, str]] = []
    current_key = "root"
    current_lines: list[str] = []
    for raw_line in content.splitlines():
        match = re.match(r"^(#{1,4})\s+(.+?)\s*$", raw_line)
        if match and current_lines:
            chunks.append((current_key, "\n".join(current_lines).strip()))
            current_lines = []
        if match:
            heading = match.group(2).strip()
            current_key = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "-",
                heading,
            ).strip("-")[:80]
            if not current_key:
                current_key = (
                    "section-"
                    + hashlib.sha1(heading.encode("utf-8")).hexdigest()[:12]
                )
        current_lines.append(raw_line)
    if current_lines:
        chunks.append((current_key, "\n".join(current_lines).strip()))
    key_counts: dict[str, int] = {}
    unique_chunks: list[tuple[str, str]] = []
    for section_key, text in chunks:
        key_counts[section_key] = key_counts.get(section_key, 0) + 1
        unique_key = (
            section_key
            if key_counts[section_key] == 1
            else f"{section_key}-{key_counts[section_key]}"
        )
        unique_chunks.append((unique_key, text))
    role_scope = _oracle_role_scope(file_key)
    return [
        _oracle_section(
            file_key=file_key,
            section_key=section_key,
            role_scope=role_scope,
            text=_oracle_trim(text, 3000),
            source_refs=[f"llm_kb:{file_key}#{section_key}"],
            source_digest=source_digest,
            chapter_number=chapter_number,
        )
        for section_key, text in unique_chunks
        if text.strip()
    ]


def _oracle_jsonl_sections(
    file_key: str,
    content: str,
    *,
    source_digest: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for index, raw in enumerate(content.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SetupBlocked("LLM KB JSONL artifact is invalid") from exc
        if not isinstance(payload, Mapping):
            raise SetupBlocked("LLM KB JSONL artifact row is not an object")
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        sections.append(
            _oracle_section(
                file_key=file_key,
                section_key=str(payload.get("id") or index),
                role_scope=_oracle_role_scope(file_key),
                text=_oracle_trim(text, 2400),
                source_refs=[f"llm_kb:{file_key}:{index}"],
                source_digest=source_digest,
                chapter_number=chapter_number,
                raw_payload=payload,
            )
        )
    return sections


def _oracle_role_sections(
    role: str,
    content: str,
    *,
    source_digest: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SetupBlocked("LLM KB role artifact is invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise SetupBlocked("LLM KB role artifact is not an object")
    file_key = f"packs/{role}/context.json"
    sections = [
        _oracle_section(
            file_key=file_key,
            section_key="context",
            role_scope=role,
            text=_oracle_trim(content, 6000),
            source_refs=[f"llm_kb:pack:{role}"],
            source_digest=source_digest,
            chapter_number=chapter_number,
        )
    ]
    contexts = payload.get("active_personality_contexts")
    if contexts is None:
        contexts = []
    if not isinstance(contexts, list):
        raise SetupBlocked("LLM KB role personality contexts are invalid")
    for index, context in enumerate(contexts, start=1):
        if not isinstance(context, Mapping):
            raise SetupBlocked("LLM KB role personality context is invalid")
        character_id = str(context.get("character_id") or index)
        text = json.dumps(
            {
                "character_id": context.get("character_id", ""),
                "character_name": context.get("character_name", ""),
                "active_skills": context.get("active_skills", {}),
                "current_behavior_bias": context.get(
                    "current_behavior_bias",
                    {},
                ),
                "constraints": context.get("constraints", []),
                "source_refs": context.get("source_refs", []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        sections.append(
            _oracle_section(
                file_key=f"packs/{role}/active_personality_context.json",
                section_key=character_id,
                role_scope=role,
                text=_oracle_trim(text, 2400),
                source_refs=[
                    f"llm_kb:pack:{role}:active_personality_context:{character_id}"
                ],
                source_digest=source_digest,
                chapter_number=chapter_number,
            )
        )
    return sections


def build_expected_llm_kb_points(
    snapshot: Mapping[str, Any],
    *,
    project_id: str,
    chapter_number: int,
) -> list[dict[str, Any]]:
    if not isinstance(snapshot, Mapping) or snapshot.get("project_id") != project_id:
        raise SetupBlocked("LLM KB artifact snapshot is not project-bound")
    raw_files = snapshot.get("files")
    if not isinstance(raw_files, list):
        raise SetupBlocked("LLM KB artifact snapshot files are missing")
    files: dict[str, str] = {}
    for row in raw_files:
        if not isinstance(row, Mapping):
            raise SetupBlocked("LLM KB artifact row is malformed")
        path = str(row.get("path") or "")
        content = row.get("content")
        if not path or path in files or not isinstance(content, str):
            raise SetupBlocked("LLM KB artifact path/content is invalid")
        encoded = content.encode("utf-8")
        if (
            type(row.get("size")) is not int
            or int(row["size"]) != len(encoded)
            or row.get("content_sha256")
            != hashlib.sha256(encoded).hexdigest()
        ):
            raise SetupBlocked("LLM KB artifact digest/size is invalid")
        files[path] = content
    index_content = files.get("retrieval_index.json")
    if index_content is None:
        raise SetupBlocked("LLM KB retrieval index artifact is missing")
    try:
        index = json.loads(index_content)
    except json.JSONDecodeError as exc:
        raise SetupBlocked("LLM KB retrieval index artifact is invalid") from exc
    if (
        not isinstance(index, Mapping)
        or index.get("project_id") != project_id
        or type(index.get("as_of_chapter")) is not int
        or int(index["as_of_chapter"]) != int(chapter_number)
        or index.get("projection_version") != LLM_KB_PROJECTION_VERSION
    ):
        raise SetupBlocked("LLM KB retrieval index target is invalid")
    source_digest = _required_text(
        index.get("source_digest"),
        "LLM KB source digest",
    )
    indexed_files = index.get("files")
    if (
        not isinstance(indexed_files, list)
        or any(not isinstance(item, str) for item in indexed_files)
        or len(indexed_files) != len(set(indexed_files))
        or set(indexed_files) != LLM_KB_ROOT_FILE_KEYS
    ):
        raise SetupBlocked("LLM KB retrieval index file set is invalid")
    role_files = {
        f"packs/{role}/context.json" for role in LLM_KB_VECTOR_ROLES
    }
    if set(files) != {"retrieval_index.json", *indexed_files, *role_files}:
        raise SetupBlocked("LLM KB artifact snapshot file set is not exact")
    sections: list[dict[str, Any]] = []
    for file_key in sorted(indexed_files):
        content = files[file_key]
        if file_key.endswith(".jsonl"):
            sections.extend(
                _oracle_jsonl_sections(
                    file_key,
                    content,
                    source_digest=source_digest,
                    chapter_number=chapter_number,
                )
            )
        else:
            sections.extend(
                _oracle_markdown_sections(
                    file_key,
                    content,
                    source_digest=source_digest,
                    chapter_number=chapter_number,
                )
            )
    for role in LLM_KB_VECTOR_ROLES:
        sections.extend(
            _oracle_role_sections(
                role,
                files[f"packs/{role}/context.json"],
                source_digest=source_digest,
                chapter_number=chapter_number,
            )
        )
    points: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for section in sections:
        if not section["text"].strip():
            continue
        point_id = _oracle_point_id(
            project_id,
            section["file_key"],
            section["section_key"],
            section["role_scope"],
        )
        if point_id in seen_ids:
            raise SetupBlocked("LLM KB artifact produced duplicate point identity")
        seen_ids.add(point_id)
        points.append(
            {
                "id": point_id,
                "payload": {"project_id": project_id, **section},
            }
        )
    if not points:
        raise SetupBlocked("LLM KB artifact produced no expected points")
    return sorted(points, key=lambda point: str(point["id"]))


def _oracle_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _oracle_point_bindings(
    projection_type: str,
    collection: str,
    points: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    bindings = []
    for point in points:
        payload = point.get("payload") if isinstance(point, Mapping) else None
        if not isinstance(payload, Mapping):
            raise SetupBlocked("Qdrant oracle expected point is malformed")
        bindings.append(
            {
                "projection_type": projection_type,
                "collection": collection,
                "raw_point_id": _required_text(
                    point.get("id"),
                    "Qdrant oracle point identity",
                ),
                "payload_sha256": _oracle_json_sha256(payload),
            }
        )
    return sorted(
        bindings,
        key=lambda row: (
            row["projection_type"],
            row["collection"],
            row["raw_point_id"],
        ),
    )


def build_qdrant_oracle_capture(
    *,
    phase: str,
    fault_id: str,
    project_id: str,
    canon_id: str,
    chapter_number: int,
    chapter_rows: Sequence[Mapping[str, Any]],
    llm_kb_snapshot: Mapping[str, Any],
    expected_points_by_projection: Mapping[
        str,
        Sequence[Mapping[str, Any]],
    ],
    collections: Mapping[str, str],
) -> dict[str, Any]:
    if phase not in QDRANT_ORACLE_PHASES:
        raise SetupBlocked("Qdrant oracle capture phase is invalid")
    if set(expected_points_by_projection) != set(QDRANT_PROJECTION_TYPES):
        raise SetupBlocked("Qdrant oracle projection set is not exact")
    if set(collections) != set(QDRANT_PROJECTION_TYPES):
        raise SetupBlocked("Qdrant oracle collection set is not exact")
    normalized_rows: list[dict[str, Any]] = []
    row_manifest: list[dict[str, Any]] = []
    for row in chapter_rows:
        if not isinstance(row, Mapping):
            raise SetupBlocked("Canon oracle row is malformed")
        normalized = {
            "project_id": row.get("project_id"),
            "chapter_number": row.get("chapter_number"),
            "title": str(row.get("title") or ""),
            "summary": str(row.get("summary") or ""),
            "body_text": str(row.get("body_text") or ""),
        }
        normalized_rows.append(normalized)
        row_manifest.append(
            {
                "project_id": normalized["project_id"],
                "chapter_number": normalized["chapter_number"],
                "title_sha256": hashlib.sha256(
                    normalized["title"].encode("utf-8")
                ).hexdigest(),
                "summary_sha256": hashlib.sha256(
                    normalized["summary"].encode("utf-8")
                ).hexdigest(),
                "body_text_sha256": hashlib.sha256(
                    normalized["body_text"].encode("utf-8")
                ).hexdigest(),
                "excerpt_sha256": hashlib.sha256(
                    normalized["body_text"][:500].encode("utf-8")
                ).hexdigest(),
            }
        )
    raw_artifacts = llm_kb_snapshot.get("files")
    if not isinstance(raw_artifacts, list):
        raise SetupBlocked("LLM KB oracle artifact list is missing")
    artifact_manifest = sorted(
        (
            {
                "path": row.get("path"),
                "size": row.get("size"),
                "content_sha256": row.get("content_sha256"),
            }
            for row in raw_artifacts
            if isinstance(row, Mapping)
        ),
        key=lambda row: str(row["path"]),
    )
    if len(artifact_manifest) != len(raw_artifacts):
        raise SetupBlocked("LLM KB oracle artifact row is malformed")
    point_bindings = [
        binding
        for projection_type in QDRANT_PROJECTION_TYPES
        for binding in _oracle_point_bindings(
            projection_type,
            _required_text(
                collections[projection_type],
                f"{projection_type} oracle collection",
            ),
            expected_points_by_projection[projection_type],
        )
    ]
    point_bindings.sort(
        key=lambda row: (
            row["projection_type"],
            row["collection"],
            row["raw_point_id"],
        )
    )
    return {
        "phase": phase,
        "fault_id": fault_id,
        "project_id": project_id,
        "canon_id": canon_id,
        "chapter_number": int(chapter_number),
        "canon_oracle": {
            "row_count": len(normalized_rows),
            "source_rows_sha256": _oracle_json_sha256(normalized_rows),
            "row_manifest_sha256": _oracle_json_sha256(row_manifest),
            "rows": row_manifest,
        },
        "llm_kb_oracle": {
            "root": llm_kb_snapshot.get("root"),
            "artifact_count": len(artifact_manifest),
            "artifacts_sha256": _oracle_json_sha256(artifact_manifest),
            "artifacts": artifact_manifest,
        },
        "expected_point_count": len(point_bindings),
        "expected_points_sha256": _oracle_json_sha256(
            {
                projection_type: list(
                    expected_points_by_projection[projection_type]
                )
                for projection_type in QDRANT_PROJECTION_TYPES
            }
        ),
        "point_bindings_sha256": _oracle_json_sha256(point_bindings),
        "point_bindings": point_bindings,
    }


def _qdrant_point_evidence_identity(
    point_id: str,
    payload: Mapping[str, Any],
    vector: Sequence[float],
) -> str:
    payload_sha256 = hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    vector_sha256 = hashlib.sha256(
        canonical_json(list(vector)).encode("utf-8")
    ).hexdigest()
    return (
        f"{point_id}#payload-sha256={payload_sha256}"
        f"#vector-sha256={vector_sha256}"
    )


def _qdrant_vector(point: Mapping[str, Any]) -> list[float]:
    raw = point.get("vector")
    if not isinstance(raw, Sequence) or isinstance(
        raw,
        (str, bytes, bytearray),
    ):
        raise SetupBlocked("Qdrant projection point vector is missing")
    vector: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SetupBlocked("Qdrant projection point vector is invalid")
        try:
            normalized = float(value)
        except (OverflowError, ValueError) as exc:
            raise SetupBlocked(
                "Qdrant projection point vector is invalid"
            ) from exc
        if not math.isfinite(normalized):
            raise SetupBlocked("Qdrant projection point vector is not finite")
        vector.append(normalized)
    if not vector:
        raise SetupBlocked("Qdrant projection point vector is empty")
    if not any(value != 0.0 for value in vector):
        raise SetupBlocked("Qdrant projection point vector is degenerate")
    return vector


def _expected_qdrant_payloads(
    raw_points: Sequence[Mapping[str, Any]],
    *,
    projection_type: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_points, Sequence) or isinstance(
        raw_points,
        (str, bytes, bytearray),
    ):
        raise SetupBlocked(
            f"expected {projection_type} Qdrant points are not an array"
        )
    if not raw_points:
        raise SetupBlocked(
            f"expected {projection_type} Qdrant projection contains no points"
        )
    expected: dict[str, dict[str, Any]] = {}
    for point in raw_points:
        if not isinstance(point, Mapping):
            raise SetupBlocked("expected Qdrant projection point is malformed")
        raw_point_id = point.get("id")
        if isinstance(raw_point_id, bool) or not isinstance(
            raw_point_id,
            (str, int),
        ):
            raise SetupBlocked("expected Qdrant point identity is invalid")
        point_id = str(raw_point_id).strip()
        payload = point.get("payload")
        if not point_id or not isinstance(payload, Mapping):
            raise SetupBlocked("expected Qdrant point payload is missing")
        if point_id in expected:
            raise SetupBlocked("expected Qdrant point identity is duplicated")
        expected[point_id] = dict(payload)
    return expected


def normalize_qdrant_projections(
    *,
    status: Mapping[str, Any],
    points_by_projection: Mapping[str, Sequence[Mapping[str, Any]]],
    expected_points_by_projection: Mapping[
        str,
        Sequence[Mapping[str, Any]],
    ],
    project_id: str,
    canon_id: str,
    chapter_number: int,
    collections: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected = set(QDRANT_PROJECTION_TYPES)
    if set(collections) != expected:
        raise SetupBlocked("Qdrant projection collection set is not exact")
    if set(points_by_projection) != expected:
        raise SetupBlocked("Qdrant point projection set is not exact")
    if set(expected_points_by_projection) != expected:
        raise SetupBlocked("expected Qdrant point projection set is not exact")
    normalized_collections = {
        projection_type: _required_text(
            collections[projection_type],
            f"{projection_type} Qdrant collection",
        )
        for projection_type in QDRANT_PROJECTION_TYPES
    }
    if len(set(normalized_collections.values())) != len(expected):
        raise SetupBlocked("Qdrant projection collections must be distinct")
    chapter_number = int(chapter_number)
    if chapter_number <= 0:
        raise SetupBlocked("Qdrant projection chapter number is invalid")
    healthy = _healthy_components(status, canon_id=canon_id)
    if any(kind not in healthy for kind in QDRANT_PROJECTION_TYPES):
        raise SetupBlocked("Qdrant projection checkpoint is not converged")
    projections: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    raw_identities: set[tuple[str, str]] = set()
    for projection_type in QDRANT_PROJECTION_TYPES:
        raw_points = points_by_projection[projection_type]
        expected_payloads = _expected_qdrant_payloads(
            expected_points_by_projection[projection_type],
            projection_type=projection_type,
        )
        if not isinstance(raw_points, Sequence) or isinstance(
            raw_points,
            (str, bytes, bytearray),
        ):
            raise SetupBlocked(
                f"{projection_type} Qdrant points are not an array"
            )
        if not raw_points:
            raise SetupBlocked(
                f"{projection_type} Qdrant projection contains no points"
            )
        collection = normalized_collections[projection_type]
        observed_point_ids: set[str] = set()
        observed_vector_dimensions: set[int] = set()
        for point in raw_points:
            if not isinstance(point, Mapping):
                raise SetupBlocked("Qdrant projection contains a malformed point")
            raw_point_id = point.get("id")
            if isinstance(raw_point_id, bool) or not isinstance(
                raw_point_id,
                (str, int),
            ):
                raise SetupBlocked("Qdrant projection point identity is invalid")
            point_id = str(raw_point_id).strip()
            if not point_id:
                raise SetupBlocked("Qdrant projection point identity is empty")
            identity = (collection, point_id)
            if identity in raw_identities:
                raise SetupBlocked("Qdrant duplicate point identity was observed")
            raw_identities.add(identity)
            observed_point_ids.add(point_id)
            payload = point.get("payload")
            if not isinstance(payload, Mapping):
                raise SetupBlocked("Qdrant projection point payload is missing")
            if payload.get("project_id") != project_id:
                raise SetupBlocked("Qdrant projection point project is not bound")
            if projection_type == "chapter_memory":
                if (
                    type(payload.get("chapter_number")) is not int
                    or not 1 <= int(payload["chapter_number"]) <= chapter_number
                ):
                    raise SetupBlocked(
                        "chapter_memory Qdrant payload chapter is not bound"
                    )
            elif (
                payload.get("index_kind") != "llm_kb"
                or type(payload.get("as_of_chapter")) is not int
                or int(payload["as_of_chapter"]) != chapter_number
            ):
                raise SetupBlocked("llm_kb Qdrant payload target is not bound")
            expected_payload = expected_payloads.get(point_id)
            if expected_payload is None:
                raise SetupBlocked(
                    f"{projection_type} Qdrant point identity is not expected"
                )
            if dict(payload) != expected_payload:
                raise SetupBlocked(
                    f"{projection_type} Qdrant point does not match expected payload"
                )
            vector = _qdrant_vector(point)
            observed_vector_dimensions.add(len(vector))
            payload_sha256 = hashlib.sha256(
                canonical_json(payload).encode("utf-8")
            ).hexdigest()
            vector_sha256 = hashlib.sha256(
                canonical_json(vector).encode("utf-8")
            ).hexdigest()
            evidence_identity = _qdrant_point_evidence_identity(
                point_id,
                payload,
                vector,
            )
            projections.append(
                {
                    "projection_type": projection_type,
                    "identity_id": evidence_identity,
                    "canon_id": canon_id,
                    "status": "converged",
                    "collection": collection,
                    "raw_point_id": point_id,
                    "payload_sha256": payload_sha256,
                    "vector_sha256": vector_sha256,
                    "vector_dimensions": len(vector),
                }
            )
            identities.append(
                {
                    "collection": collection,
                    "projection_type": projection_type,
                    "point_id": evidence_identity,
                    "raw_point_id": point_id,
                    "canon_id": canon_id,
                    "payload_sha256": payload_sha256,
                    "vector_sha256": vector_sha256,
                    "vector_dimensions": len(vector),
                }
            )
        if observed_point_ids != set(expected_payloads):
            raise SetupBlocked(
                f"{projection_type} Qdrant point identity set is not exact"
            )
        if len(observed_vector_dimensions) != 1:
            raise SetupBlocked(
                f"{projection_type} Qdrant vector dimensions are inconsistent"
            )
    projections.sort(
        key=lambda row: (
            row["projection_type"],
            row["collection"],
            row["identity_id"],
        )
    )
    identities.sort(
        key=lambda row: (
            row["projection_type"],
            row["collection"],
            row["point_id"],
        )
    )
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
    endpoint_identity: Mapping[str, Any] | None = None,
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

CHAPTER_MEMORY_ORACLE_SQL = """
    SELECT
        plans.project_id,
        plans.chapter_number,
        plans.title,
        drafts.summary,
        drafts.body_text
    FROM canon_commit_records AS commits
    JOIN candidate_draft_records AS candidates
      ON candidates.id = commits.candidate_id
    JOIN chapter_plans AS plans
      ON plans.id = candidates.chapter_plan_id
    JOIN chapter_drafts AS drafts
      ON drafts.id = candidates.candidate_draft_id
    WHERE commits.project_id = %s
      AND commits.chapter_number <= %s
      AND commits.status = 'committed'
      AND candidates.status = 'accepted'
      AND plans.status = 'accepted'
    ORDER BY plans.chapter_number
    /* task4 chapter memory oracle */
"""

FIXTURE_BOUNDARY_SQL = """
    SELECT
        chapters.id AS chapter_id,
        candidates.id AS candidate_id,
        tasks.id AS task_id,
        tasks.status AS task_status
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
        self._bound_endpoint_identity: dict[str, Any] | None = None

    def bind_endpoint_identity(
        self,
        endpoint_identity: Mapping[str, Any],
    ) -> None:
        if self._bound_endpoint_identity is not None:
            raise SetupBlocked("Task 4 endpoint identity was already bound")
        value = dict(endpoint_identity)
        if not value:
            raise SetupBlocked("Task 4 endpoint identity is empty")
        self._bound_endpoint_identity = value

    def _endpoint_identity(self) -> dict[str, Any]:
        if self._bound_endpoint_identity is None:
            raise SetupBlocked(
                "Task 4 endpoint identity was not bound before snapshot"
            )
        return dict(self._bound_endpoint_identity)

    def read_recovery_sentinel(self) -> dict[str, str]:
        rows = self.source.fetch_all(
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
            endpoint_identity=self._endpoint_identity(),
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
            endpoint_identity=self._endpoint_identity(),
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

    def chapter_memory_oracle_rows(
        self,
        fixture: FixtureContext,
    ) -> list[dict[str, Any]]:
        return self.source.fetch_all(
            CHAPTER_MEMORY_ORACLE_SQL,
            (fixture.project_id, fixture.chapter_number),
        )

    def wait_candidate_fixture(
        self,
        *,
        fault_id: str,
        fixture_id: str,
        project_id: str,
        task_id: str,
        timeout_seconds: float = GENERATION_BOUNDARY_TIMEOUT_SECONDS,
        poll_seconds: float = 1.0,
    ) -> FixtureContext:
        return self._wait_fixture_boundary(
            fault_id=fault_id,
            fixture_id=fixture_id,
            project_id=project_id,
            task_id=task_id,
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
                if chapter_id and candidate_id:
                    return FixtureContext(
                        fixture_id=fixture_id,
                        fault_id=fault_id,
                        project_id=project_id,
                        chapter_number=1,
                        chapter_id=chapter_id,
                        task_id=task_id,
                        candidate_id=candidate_id,
                    )
                task_status = str(row.get("task_status") or "").strip()
                if task_status in GENERATION_TASK_TERMINAL_STATUSES:
                    raise SetupBlocked(
                        "generation task reached terminal status "
                        f"{task_status} before candidate fixture"
                    )
            if time.monotonic() < deadline:
                time.sleep(poll_seconds)
        raise SetupBlocked("one-chapter fixture did not reach candidate fixture")

    def generation_barrier_stop_reason(self, fixture: FixtureContext) -> str:
        status = str(self._task_row(fixture).get("status") or "").strip()
        if status not in GENERATION_TASK_TERMINAL_STATUSES:
            return ""
        return (
            f"generation task reached terminal status {status} "
            "before the Canon barrier"
        )

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
        collections: Mapping[str, str],
        transport: Callable[..., dict[str, Any]] = http_json,
    ) -> None:
        self.qdrant_url = required_url(qdrant_url, "Qdrant URL")
        if set(collections) != set(QDRANT_PROJECTION_TYPES):
            raise RunnerError("Qdrant projection collection set is not exact")
        self.collections = {
            projection_type: _required_text(
                collections[projection_type],
                f"{projection_type} Qdrant collection",
            )
            for projection_type in QDRANT_PROJECTION_TYPES
        }
        if len(set(self.collections.values())) != len(self.collections):
            raise RunnerError("Qdrant projection collections must be distinct")
        self.transport = transport

    def project_points_by_projection(
        self,
        project_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            projection_type: self._project_points(
                project_id,
                collection,
            )
            for projection_type, collection in self.collections.items()
        }

    def _project_points(
        self,
        project_id: str,
        collection_name: str,
    ) -> list[dict[str, Any]]:
        collection = urllib.parse.quote(collection_name, safe="")
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
                "with_vector": True,
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
        api_url: str,
        mcp_url: str,
        database_url: str,
        qdrant_url: str = "",
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
        self.api_url = api_url
        self.mcp_url = mcp_url
        self.database_url = database_url
        self.qdrant_url = qdrant_url
        self.barrier_factory = barrier_factory
        self.stage = "initial"
        self.stack_started = False
        self.faulted = False
        self.recovered = False
        self.barrier: Any | None = None
        self.barrier_observation: BarrierObservation | None = None
        self.fixture: FixtureContext | None = None
        self.supplemental_artifacts: dict[str, dict[str, Any]] = {}
        self.qdrant_oracle_captures: list[dict[str, Any]] = []

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
            if self.fault_kind in PROJECTION_FAULTS:
                common.require_client_endpoint(
                    self.api,
                    attribute="api_url",
                    expected_url=self.api_url,
                    label="API",
                )
            if self.fault_kind == "qdrant_unavailable" and not self.qdrant_url:
                raise SetupBlocked(
                    "Qdrant client endpoint is missing from endpoint binding"
                )
            if self.fault_kind == "qdrant_unavailable":
                common.require_client_endpoint(
                    self.qdrant,
                    attribute="qdrant_url",
                    expected_url=self.qdrant_url,
                    label="Qdrant",
                )
            endpoint_identity = common.bind_recovery_endpoints(
                controller=self.controller,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                api_url=self.api_url,
                mcp_url=self.mcp_url,
                database_url=self.database_url,
                sentinel_reader=self.sql.read_recovery_sentinel,
                qdrant_url=(
                    self.qdrant_url
                    if self.fault_kind == "qdrant_unavailable"
                    else None
                ),
            )
            self.sql.bind_endpoint_identity(endpoint_identity)
            if self.fault_kind in GENERATION_FAULTS:
                snapshots = self._run_generation_fault()
            else:
                snapshots = self._run_projection_fault()
        except BaseException as exc:
            failure = exc
        finally:
            interrupted = failure is not None and not isinstance(
                failure, Exception
            )
            if (
                not interrupted
                and self.fault_kind in GENERATION_FAULTS
                and self.barrier is not None
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
                    if not isinstance(exc, Exception):
                        failure = exc
            if self.barrier is not None:
                try:
                    self.barrier.cleanup()
                except BaseException as exc:
                    cleanup_errors.append(f"barrier cleanup: {exc}")
                    if not isinstance(exc, Exception):
                        failure = exc
                else:
                    if failure is None or isinstance(failure, Exception):
                        try:
                            self._capture_barrier_evidence()
                        except BaseException as exc:
                            cleanup_errors.append(
                                f"barrier evidence: {exc}"
                            )
                            if not isinstance(exc, Exception):
                                failure = exc
            if failure is not None and not isinstance(failure, Exception):
                try:
                    self.controller.interrupt_cleanup(self.fault_id)
                except BaseException as exc:
                    cleanup_errors.append(f"interrupt cleanup: {exc}")
            else:
                cleanup_errors.extend(self._cleanup_stack())

        if failure is not None and not isinstance(failure, Exception):
            raise failure.with_traceback(failure.__traceback__)
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
        fixture = self.sql.wait_candidate_fixture(
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
        self.barrier_observation = self.barrier.wait_for_blocked_waiter(
            timeout_seconds=GENERATION_BOUNDARY_TIMEOUT_SECONDS,
            stop_reason=lambda: self.sql.generation_barrier_stop_reason(fixture)
        )
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
                "candidate_id": self.fixture.candidate_id,
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
        if self.barrier_factory is None:
            raise SetupBlocked("projection Canon barrier factory is missing")
        self.stage = "barrier_install"
        self.barrier = self.barrier_factory()
        self.barrier.install(project_id=project.project_id, chapter_number=1)
        self.stage = "writing_handoff"
        task = asyncio.run(self.lifecycle.start_writing(project.project_id))
        self.stage = "candidate_ready"
        fixture = self.sql.wait_candidate_fixture(
            fault_id=self.fault_id,
            fixture_id=f"fixture-{self.fault_id}",
            project_id=project.project_id,
            task_id=task.task_id,
        )
        self.stage = "barrier_wait"
        self.barrier_observation = self.barrier.wait_for_blocked_waiter(
            timeout_seconds=GENERATION_BOUNDARY_TIMEOUT_SECONDS,
            stop_reason=lambda: self.sql.generation_barrier_stop_reason(fixture)
        )
        self.fixture = fixture
        service = SERVICE_BY_FAULT[self.fault_kind]
        self.stage = "service_fault"
        self.controller.stop(service, self.fault_id)
        self.faulted = True
        self.stage = "barrier_cleanup"
        self.barrier.cleanup()
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
            oracle_phase="replay_baseline",
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
            oracle_phase="final",
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
        oracle_phase: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self.fault_kind == "qdrant_unavailable":
            if self.qdrant is None:
                raise SetupBlocked("Qdrant reader is missing")
            chapter_rows = self.sql.chapter_memory_oracle_rows(fixture)
            llm_kb_snapshot = self.controller.llm_kb_artifact_snapshot(
                self.fault_id,
                fixture.project_id,
            )
            expected_points = {
                "chapter_memory": build_expected_chapter_memory_points(
                    chapter_rows,
                    project_id=fixture.project_id,
                    chapter_number=fixture.chapter_number,
                ),
                "llm_kb": build_expected_llm_kb_points(
                    llm_kb_snapshot,
                    project_id=fixture.project_id,
                    chapter_number=fixture.chapter_number,
                ),
            }
            normalized = normalize_qdrant_projections(
                status=status,
                points_by_projection=(
                    self.qdrant.project_points_by_projection(
                        fixture.project_id
                    )
                ),
                expected_points_by_projection=expected_points,
                project_id=fixture.project_id,
                canon_id=fixture.canon_id,
                chapter_number=fixture.chapter_number,
                collections=self.qdrant.collections,
            )
            capture = build_qdrant_oracle_capture(
                phase=_required_text(oracle_phase, "Qdrant oracle phase"),
                fault_id=self.fault_id,
                project_id=fixture.project_id,
                canon_id=fixture.canon_id,
                chapter_number=fixture.chapter_number,
                chapter_rows=chapter_rows,
                llm_kb_snapshot=llm_kb_snapshot,
                expected_points_by_projection=expected_points,
                collections=self.qdrant.collections,
            )
            if len(self.qdrant_oracle_captures) >= len(QDRANT_ORACLE_PHASES):
                raise SetupBlocked("Qdrant oracle capture count exceeded")
            self.qdrant_oracle_captures.append(capture)
            self.supplemental_artifacts[QDRANT_ORACLE_ARTIFACT_NAME] = {
                "schema_version": 1,
                "fault_kind": self.fault_kind,
                "fault_id": self.fault_id,
                "captures": list(self.qdrant_oracle_captures),
            }
            return normalized
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
    qdrant_collections: tuple[tuple[str, str], ...] = ()


def candidate_runtime_qdrant_projection_collections() -> dict[str, str]:
    stack = common.load_module(
        "forwin_recovery_stack_collection_resolver",
        CONTROLLER_PATH,
    )
    try:
        collections = stack.effective_qdrant_projection_collections()
    except Exception as exc:
        raise RunnerError(
            "candidate runtime Qdrant projection collections could not be "
            "resolved: "
            f"{exc}"
        ) from exc
    if not isinstance(collections, Mapping):
        raise RunnerError(
            "candidate runtime Qdrant projection collections are invalid"
        )
    return {
        str(key): str(value)
        for key, value in collections.items()
    }


def candidate_runtime_llm_kb_qdrant_collection() -> str:
    return _required_text(
        candidate_runtime_qdrant_projection_collections().get("llm_kb"),
        "candidate runtime llm_kb Qdrant collection",
    )


def resolve_run_config(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] = os.environ,
    qdrant_collection_resolver: Callable[
        [], Mapping[str, str]
    ] | None = None,
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
    qdrant_collections: tuple[tuple[str, str], ...] = ()
    if fault_kind == "qdrant_unavailable":
        qdrant_url = str(
            environ.get("FORWIN_RECOVERY_QDRANT_URL") or ""
        ).strip()
        if not qdrant_url:
            raise RunnerError(
                "FORWIN_RECOVERY_QDRANT_URL is required for the Qdrant fault"
            )
        qdrant_url = required_url(qdrant_url, "Qdrant URL")
        for override in QDRANT_AMBIENT_COLLECTION_OVERRIDES:
            if str(environ.get(override) or "").strip():
                raise RunnerError(
                    f"{override} must not be set; the Qdrant fault derives "
                    "collections from the exact candidate runtime"
                )
        resolver = (
            qdrant_collection_resolver
            or candidate_runtime_qdrant_projection_collections
        )
        resolved = resolver()
        if not isinstance(resolved, Mapping) or set(resolved) != set(
            QDRANT_PROJECTION_TYPES
        ):
            raise RunnerError(
                "candidate runtime Qdrant projection collection set is not "
                "exact"
            )
        normalized = {
            projection_type: _required_text(
                resolved[projection_type],
                f"candidate runtime {projection_type} Qdrant collection",
            )
            for projection_type in QDRANT_PROJECTION_TYPES
        }
        if len(set(normalized.values())) != len(normalized):
            raise RunnerError(
                "candidate runtime Qdrant projection collections must be "
                "distinct"
            )
        qdrant_collections = tuple(normalized.items())
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
        qdrant_collections=qdrant_collections,
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
            collections=dict(config.qdrant_collections),
        )
        if config.fault_kind == "qdrant_unavailable"
        else None
    )
    barrier_factory: Callable[[], AdvisoryBarrier] | None = None
    if config.fault_kind in GENERATION_FAULTS | PROJECTION_FAULTS:
        barrier_kind = (
            config.fault_kind
            if config.fault_kind in GENERATION_FAULTS
            else "generation_worker_precommit_crash"
        )
        barrier_factory = lambda: AdvisoryBarrier(
            kind=barrier_kind,
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
        writer=EvidenceWriter(
            evidence_dir=config.evidence_dir,
            runner_path=Path(__file__),
        ),
        api_url=config.api_url,
        mcp_url=config.mcp_url,
        database_url=config.database_url,
        qdrant_url=config.qdrant_url,
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
