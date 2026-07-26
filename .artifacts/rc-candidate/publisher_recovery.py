#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import base64
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from psycopg import sql


ARTIFACT_DIR = Path(__file__).resolve().parent
if str(ARTIFACT_DIR) not in sys.path:
    sys.path.insert(0, str(ARTIFACT_DIR))

from recovery_runner_common import (  # noqa: E402
    CandidateIdentity,
    EvidenceWriter,
    RecoveryController,
    RunnerError,
    SetupBlocked,
    atomic_write_json_new,
    bind_recovery_endpoints,
    candidate_identity,
    http_json,
    normalize_database_url,
    psycopg_connect,
    require_client_endpoint,
    required_url,
    stable_hash,
    validate_fault_id,
)


SUPPORTED_FAULTS = (
    "publisher_backend_unavailable",
    "publisher_browser_unavailable",
    "publisher_captcha",
    "publisher_mfa",
    "publisher_account_risk",
)
RISK_REASONS = {
    "publisher_captcha": "captcha",
    "publisher_mfa": "mfa",
    "publisher_account_risk": "account_risk",
}
FIXTURE_PLATFORM = "qidian"
FIXTURE_BOOK_NAME = "Publisher Recovery Fixture"
FIXTURE_CHAPTER_TITLE = "Recovery Chapter"
FIXTURE_BODY = "Generic publisher recovery fixture content."
OPERATOR_REASON = "Task 6 deterministic publisher recovery proof."
RISK_BOUNDARY = "pre-mutation"
PUBLISHER_WORKER_APPLICATION_NAME = "forwin-recovery-publisher-worker"
PUBLISHER_COVER_ROOT = "/app/data/publisher_covers"
ENV_NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
FIXTURE_COVER_PAYLOAD = {
    "book_meta": {
        "intro": "Generic recovery cover fixture.",
        "primary_category": "systems",
    },
    "auto_cover_upload_enabled": False,
    "cover_candidate_count": 1,
    "cover_confirmation_required": False,
}
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "api_key",
)


@dataclass(frozen=True, slots=True)
class PublisherFixture:
    fixture_id: str
    fault_kind: str
    fault_id: str
    job_id: str
    logical_key: str
    task_kind: str
    platform_id: str
    project_id: str
    book_name: str
    chapter_title: str
    body: str
    body_sha256: str
    publish: bool
    result_payload: dict[str, Any]

    def evidence_identity(self) -> dict[str, str]:
        return {
            "fixture_id": self.fixture_id,
            "fault_id": self.fault_id,
            "resource_type": "publisher_job",
            "resource_id": self.job_id,
            "logical_key": self.logical_key,
        }

    def with_changes(self, **changes: Any) -> PublisherFixture:
        return replace(self, **changes)


def _fixture_suffix(fault_id: str) -> str:
    return hashlib.sha256(fault_id.encode("ascii")).hexdigest()[:24]


def publisher_fixture(fault_kind: str, fault_id: str) -> PublisherFixture:
    if fault_kind not in SUPPORTED_FAULTS:
        raise RunnerError(f"unsupported Task 6 fault: {fault_kind}")
    normalized_fault_id = validate_fault_id(fault_id)
    suffix = _fixture_suffix(normalized_fault_id)
    backend = fault_kind == "publisher_backend_unavailable"
    body = "" if backend else FIXTURE_BODY
    fixture = PublisherFixture(
        fixture_id=f"publisher-recovery-fixture-{suffix}",
        fault_kind=fault_kind,
        fault_id=normalized_fault_id,
        job_id=f"publisher-recovery-job-{suffix}",
        logical_key=f"publisher-recovery:v1:{normalized_fault_id}",
        task_kind="cover_generate" if backend else "chapter_upload",
        platform_id=FIXTURE_PLATFORM,
        project_id="",
        book_name=FIXTURE_BOOK_NAME,
        chapter_title="" if backend else FIXTURE_CHAPTER_TITLE,
        body=body,
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        publish=False,
        result_payload=(
            json.loads(json.dumps(FIXTURE_COVER_PAYLOAD))
            if backend
            else {}
        ),
    )
    validate_fixture_spec(fixture)
    return fixture


def _sensitive_paths(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SENSITIVE_KEY_FRAGMENTS):
                found.append(path)
            found.extend(_sensitive_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_sensitive_paths(nested, f"{prefix}[{index}]"))
    return found


def validate_fixture_spec(fixture: PublisherFixture) -> None:
    if fixture.fault_kind not in SUPPORTED_FAULTS:
        raise SetupBlocked("publisher recovery fixture fault kind is unsupported")
    suffix = _fixture_suffix(validate_fault_id(fixture.fault_id))
    if (
        fixture.fixture_id != f"publisher-recovery-fixture-{suffix}"
        or fixture.job_id != f"publisher-recovery-job-{suffix}"
    ):
        raise SetupBlocked("publisher recovery fixture identity drifted")
    if fixture.project_id:
        raise SetupBlocked("publisher recovery fixture must be projectless")
    if fixture.publish:
        raise SetupBlocked("publisher recovery fixture must not publish")
    backend = fixture.fault_kind == "publisher_backend_unavailable"
    if fixture.task_kind != ("cover_generate" if backend else "chapter_upload"):
        raise SetupBlocked("publisher recovery fixture task kind drifted")
    if (
        fixture.platform_id != FIXTURE_PLATFORM
        or fixture.book_name != FIXTURE_BOOK_NAME
        or fixture.chapter_title
        != ("" if backend else FIXTURE_CHAPTER_TITLE)
        or fixture.body != ("" if backend else FIXTURE_BODY)
    ):
        raise SetupBlocked("publisher recovery fixture fixed content drifted")
    if fixture.logical_key != f"publisher-recovery:v1:{fixture.fault_id}":
        raise SetupBlocked("publisher recovery fixture logical key drifted")
    if fixture.body_sha256 != hashlib.sha256(
        fixture.body.encode("utf-8")
    ).hexdigest():
        raise SetupBlocked("publisher recovery fixture body hash drifted")
    sensitive = _sensitive_paths(fixture.result_payload)
    if sensitive:
        raise SetupBlocked(
            "publisher recovery fixture stores credential material: "
            + ", ".join(sensitive)
        )
    payload_text = json.dumps(fixture.result_payload, sort_keys=True).lower()
    if "project_id" in fixture.result_payload:
        raise SetupBlocked("publisher recovery fixture payload stores project ID")
    if "receipt" in payload_text:
        raise SetupBlocked("publisher recovery fixture stores an external receipt")
    expected_payload = FIXTURE_COVER_PAYLOAD if backend else {}
    if fixture.result_payload != expected_payload:
        raise SetupBlocked("publisher recovery fixture result payload drifted")


def fixture_insert(
    fixture: PublisherFixture,
) -> tuple[str, dict[str, Any]]:
    validate_fixture_spec(fixture)
    statement = """
        INSERT INTO publisher_upload_jobs (
            id,
            project_id,
            candidate_id,
            chapter_number,
            idempotency_key,
            platform_id,
            task_kind,
            status,
            book_name,
            chapter_title,
            body_text,
            body_sha256,
            upload_url,
            publish,
            abort_requested,
            extension_client_id,
            current_attempt_id,
            available_at,
            pause_reason,
            result_payload_json
        ) VALUES (
            %(id)s,
            %(project_id)s,
            '',
            0,
            %(idempotency_key)s,
            %(platform_id)s,
            %(task_kind)s,
            'pending',
            %(book_name)s,
            %(chapter_title)s,
            %(body_text)s,
            %(body_sha256)s,
            '',
            %(publish)s,
            false,
            '',
            '',
            now() + make_interval(secs => %(preclaim_delay_seconds)s),
            '',
            %(result_payload_json)s
        )
    """
    return statement, {
        "id": fixture.job_id,
        "project_id": fixture.project_id,
        "idempotency_key": fixture.logical_key,
        "platform_id": fixture.platform_id,
        "task_kind": fixture.task_kind,
        "book_name": fixture.book_name,
        "chapter_title": fixture.chapter_title,
        "body_text": fixture.body,
        "body_sha256": fixture.body_sha256,
        "publish": fixture.publish,
        "preclaim_delay_seconds": (
            3600
            if fixture.fault_kind == "publisher_browser_unavailable"
            else 0
        ),
        "result_payload_json": json.dumps(
            fixture.result_payload,
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def validate_stale_token_response(
    *,
    job_id: str,
    stale_owner_token: str,
    current_owner_token: str,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not str(stale_owner_token or "")
        or not str(current_owner_token or "")
        or stale_owner_token == current_owner_token
    ):
        raise SetupBlocked("publisher owner token identity did not advance")
    if dict(response) != {"ok": False, "stale_claim": True}:
        raise SetupBlocked(
            "production publisher service accepted the stale owner token"
        )
    return {
        "observation_id": (
            "publisher-stale-token-"
            + hashlib.sha256(
                f"{job_id}:{stale_owner_token}:{current_owner_token}".encode(
                    "utf-8"
                )
            ).hexdigest()[:24]
        ),
        "job_id": str(job_id),
        "stale_owner_token": str(stale_owner_token),
        "current_owner_token": str(current_owner_token),
        "response": dict(response),
    }


class PublisherAPI:
    def __init__(
        self,
        *,
        api_url: str,
        extension_key: str,
        operator_username: str,
        operator_password: str,
        transport: Callable[..., dict[str, Any]] = http_json,
    ) -> None:
        self.api_url = str(api_url).rstrip("/")
        self.extension_key = str(extension_key or "")
        self.operator_username = str(operator_username or "")
        self.operator_password = str(operator_password or "")
        self.transport = transport
        if not self.extension_key:
            raise SetupBlocked("publisher extension key is empty")
        if not self.operator_username or not self.operator_password:
            raise SetupBlocked("publisher operator Basic credentials are empty")

    @property
    def extension_headers(self) -> dict[str, str]:
        return {"X-Forwin-Extension-Key": self.extension_key}

    @property
    def operator_headers(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{self.operator_username}:{self.operator_password}".encode(
                "utf-8"
            )
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def claim(
        self,
        fixture: PublisherFixture,
        client_id: str,
    ) -> dict[str, Any]:
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            raise SetupBlocked("publisher recovery client identity is empty")
        payload = self.transport(
            "POST",
            f"{self.api_url}/api/publishers/extension/upload-jobs/claim",
            json_body={
                "client_id": normalized_client_id,
                "connected_platforms": [fixture.platform_id],
            },
            headers=self.extension_headers,
        )
        claim = payload.get("claim")
        if payload.get("found") is not True or not isinstance(claim, Mapping):
            raise SetupBlocked("extension claim did not return the fixture job")
        job = claim.get("job")
        attempt = claim.get("attempt")
        if not isinstance(job, Mapping) or not isinstance(attempt, Mapping):
            raise SetupBlocked("extension claim response is malformed")
        expected_job = {
            "job_id": fixture.job_id,
            "idempotency_key": fixture.logical_key,
            "task_kind": fixture.task_kind,
            "platform": fixture.platform_id,
            "content_sha256": fixture.body_sha256,
        }
        if any(job.get(key) != value for key, value in expected_job.items()):
            raise SetupBlocked("extension claim job identity drifted")
        upload_input = job.get("input")
        expected_input = {
            "book_name": fixture.book_name,
            "chapter_title": fixture.chapter_title,
            "body": fixture.body,
            "publish": False,
            "create_if_missing": False,
            "upload_url": None,
            "book_meta": None,
        }
        if (
            not isinstance(upload_input, Mapping)
            or dict(upload_input) != expected_input
        ):
            raise SetupBlocked("extension claim fixture content drifted")
        if (
            claim.get("execution_mode") != "execute"
            or attempt.get("phase") != "claimed"
        ):
            raise SetupBlocked("extension claim is not at the pre-mutation boundary")
        if (
            not str(attempt.get("attempt_id") or "")
            or int(attempt.get("attempt_number") or 0) != 1
            or int(attempt.get("lease_epoch") or 0) < 1
        ):
            raise SetupBlocked("extension claim attempt fence is invalid")
        return {
            "client_id": normalized_client_id,
            "job": dict(job),
            "attempt": dict(attempt),
        }

    def pause(
        self,
        fixture: PublisherFixture,
        claim: Mapping[str, Any],
        *,
        risk_reason: str,
        observed_at: str,
    ) -> dict[str, Any]:
        expected_reason = RISK_REASONS.get(fixture.fault_kind)
        if risk_reason != expected_reason:
            raise SetupBlocked("publisher typed risk reason drifted")
        job = claim.get("job")
        attempt = claim.get("attempt")
        client_id = str(claim.get("client_id") or "")
        if not isinstance(job, Mapping) or not isinstance(attempt, Mapping):
            raise SetupBlocked("publisher pause claim is malformed")
        attempt_id = str(attempt.get("attempt_id") or "")
        lease_epoch = int(attempt.get("lease_epoch") or 0)
        if (
            job.get("job_id") != fixture.job_id
            or attempt.get("phase") != "claimed"
            or not client_id
            or not attempt_id
            or lease_epoch < 1
        ):
            raise SetupBlocked("publisher pause fence drifted")
        response = self.transport(
            "POST",
            f"{self.api_url}/api/publishers/extension/upload-jobs/"
            f"{fixture.job_id}/attempts/{attempt_id}/pause",
            json_body={
                "client_id": client_id,
                "lease_epoch": lease_epoch,
                "risk_reason": risk_reason,
                "observed_at": str(observed_at),
                "current_url": "",
                "evidence": {
                    "detector": "publisher-recovery-v1",
                    "boundary": RISK_BOUNDARY,
                    "selector": "",
                    "matched_text": "",
                    "message": "Deterministic recovery risk fixture.",
                },
            },
            headers=self.extension_headers,
        )
        if (
            response.get("disposition") != "applied"
            or response.get("pause_reason") != risk_reason
            or response.get("pause_token") != attempt_id
            or response.get("job_status") != "paused"
            or response.get("attempt_status") != "paused"
            or response.get("phase") != "claimed"
        ):
            raise SetupBlocked("publisher typed pause response drifted")
        return dict(response)

    def resume_twice(
        self,
        fixture: PublisherFixture,
        *,
        pause_token: str,
        risk_reason: str,
    ) -> dict[str, Any]:
        expected_reason = RISK_REASONS.get(fixture.fault_kind)
        if risk_reason != expected_reason:
            raise SetupBlocked("publisher typed risk reason drifted before resume")
        request = {
            "expected_pause_reason": risk_reason,
            "expected_pause_token": str(pause_token),
            "operator_reason": OPERATOR_REASON,
        }
        url = (
            f"{self.api_url}/api/publishers/upload-jobs/"
            f"{fixture.job_id}/resume"
        )
        first = self.transport(
            "POST",
            url,
            json_body=request,
            headers=self.operator_headers,
        )
        replay = self.transport(
            "POST",
            url,
            json_body=request,
            headers=self.operator_headers,
        )
        first_transition = first.get("transition")
        replay_transition = replay.get("transition")
        if (
            first.get("ok") is not True
            or replay.get("ok") is not True
            or first.get("disposition") != "applied"
            or replay.get("disposition") != "idempotent"
            or not isinstance(first_transition, Mapping)
            or not isinstance(replay_transition, Mapping)
            or first.get("job", {}).get("job_id") != fixture.job_id
            or replay.get("job", {}).get("job_id") != fixture.job_id
            or dict(first_transition) != dict(replay_transition)
            or first_transition.get("pause_token") != pause_token
            or first_transition.get("pause_reason") != risk_reason
            or first_transition.get("auth_method")
            not in {"basic", "trusted_proxy"}
            or first_transition.get("attempt_phase") != "claimed"
            or first_transition.get("old_state") != "paused"
            or first_transition.get("new_state") != "pending"
        ):
            raise SetupBlocked(
                "authenticated publisher resume/replay response drifted"
            )
        request_sha = stable_hash(request)
        transition_sha = stable_hash(dict(first_transition))
        return {
            "pause_token": str(pause_token),
            "pause_reason": risk_reason,
            "request_sha256": request_sha,
            "replay_request_sha256": request_sha,
            "first_transition_sha256": transition_sha,
            "replay_transition_sha256": stable_hash(dict(replay_transition)),
            "first_disposition": str(first["disposition"]),
            "replay_disposition": str(replay["disposition"]),
        }

    def heartbeat(self, *, client_id: str = "") -> dict[str, Any]:
        payload = self.transport(
            "GET",
            f"{self.api_url}/api/publishers/extension/heartbeat-status",
            query={
                "client_id": str(client_id),
                "stale_seconds": 90,
                "allow_latest_recent_fallback": not bool(client_id),
            },
            headers=self.extension_headers,
        )
        observed_client_id = str(payload.get("client_id") or "")
        if not observed_client_id:
            raise SetupBlocked("publisher heartbeat returned no browser identity")
        if not isinstance(payload.get("ok"), bool):
            raise SetupBlocked("publisher heartbeat health result is malformed")
        if (
            payload["ok"] is True
            and FIXTURE_PLATFORM not in (payload.get("recent_platforms") or [])
        ):
            raise SetupBlocked(
                "publisher heartbeat does not expose the fixture platform"
            )
        return {
            "browser_id": observed_client_id,
            "probe": "extension_heartbeat_status",
            "status": "healthy" if payload.get("ok") is True else "stale",
        }

    def wait_heartbeat(
        self,
        expected_status: str,
        *,
        client_id: str = "",
        timeout_seconds: float = 180.0,
        poll_seconds: float = 1.0,
    ) -> dict[str, Any]:
        if expected_status not in {"healthy", "stale"}:
            raise RunnerError("unsupported publisher heartbeat status")
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                last = self.heartbeat(client_id=client_id)
            except RunnerError:
                last = None
            if last is not None and last["status"] == expected_status:
                if client_id and last["browser_id"] != client_id:
                    raise SetupBlocked(
                        "publisher browser heartbeat identity drifted"
                    )
                return last
            time.sleep(poll_seconds)
        raise SetupBlocked(
            f"publisher heartbeat did not become {expected_status}: {last}"
        )


@dataclass(frozen=True, slots=True)
class TerminalWriteObservation:
    holder_pid: int
    waiter_pid: int
    waiter_application_name: str
    owner_token: str


def terminal_barrier_observation(
    *,
    barrier: Any,
    rows: list[Mapping[str, Any]],
    job_id: str,
    expected_owner_token: str,
    current_owner_token: str,
) -> TerminalWriteObservation:
    if str(getattr(barrier, "job_id", "") or "") != str(job_id or ""):
        raise SetupBlocked("publisher terminal barrier job identity drifted")
    holders = [
        row
        for row in rows
        if row.get("granted") is True
        and row.get("application_name") == barrier.holder_application_name
    ]
    waiters = [
        row
        for row in rows
        if row.get("granted") is False
        and row.get("wait_event_type") == "Lock"
        and barrier.target_table in str(row.get("query") or "")
    ]
    if len(rows) != 2 or len(holders) != 1 or len(waiters) != 1:
        raise SetupBlocked(
            "terminal barrier requires exactly one holder and one waiter"
        )
    waiter = waiters[0]
    if waiter.get("application_name") != PUBLISHER_WORKER_APPLICATION_NAME:
        raise SetupBlocked(
            "terminal barrier waiter is not the exact publisher-worker"
        )
    if current_owner_token != expected_owner_token:
        raise SetupBlocked("publisher backend owner token drifted at barrier")
    holder_pid = int(holders[0].get("pid") or 0)
    waiter_pid = int(waiter.get("pid") or 0)
    if (
        holder_pid < 1
        or waiter_pid < 1
        or [int(value) for value in waiter.get("blocking_pids") or []]
        != [holder_pid]
    ):
        raise SetupBlocked(
            "terminal barrier waiter is not blocked only by the scoped holder"
        )
    return TerminalWriteObservation(
        holder_pid=holder_pid,
        waiter_pid=waiter_pid,
        waiter_application_name=PUBLISHER_WORKER_APPLICATION_NAME,
        owner_token=expected_owner_token,
    )


class PsycopgDatabase:
    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[[str], Any] = psycopg_connect,
    ) -> None:
        self.database_url = normalize_database_url(database_url)
        self.connect = connect

    def query(
        self,
        statement: str,
        parameters: Any = (),
    ) -> list[dict[str, Any]]:
        with self.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return [dict(row) for row in cursor.fetchall()]

    def execute(
        self,
        statement: str,
        parameters: Any = (),
    ) -> int:
        with self.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return int(cursor.rowcount or 0)


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise SetupBlocked(
            "publisher result payload must be a valid JSON object"
        ) from exc
    if not isinstance(value, Mapping):
        raise SetupBlocked("publisher result payload must be a JSON object")
    return dict(value)


def _optional_json_object(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    return _json_object(raw)


def _unsafe_payload_paths(value: Any, prefix: str = "") -> list[str]:
    found = _sensitive_paths(value, prefix)
    if isinstance(value, Mapping):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            lowered = str(key).lower()
            if lowered == "project_id" or "receipt" in lowered:
                found.append(path)
            found.extend(_unsafe_payload_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_unsafe_payload_paths(nested, f"{prefix}[{index}]"))
    return sorted(set(found))


def _snapshot_base(
    *,
    source_sha: str,
    fault_kind: str,
    fault_id: str,
    stage: str,
    fixture: PublisherFixture,
    endpoint_identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "source_sha": source_sha,
        "fault_kind": fault_kind,
        "fault_id": fault_id,
        "stage": stage,
        "state": {
            "target": {
                "fixture": fixture.evidence_identity(),
                "endpoint_identity": dict(endpoint_identity),
            },
            "mcp": {},
            "api": {},
            "database": {},
            "external": {},
            "barrier": {},
        },
    }


class SQLCollector:
    def __init__(
        self,
        database: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.sleep = sleep
        self.monotonic = monotonic
        self._bound_endpoint_identity: dict[str, Any] | None = None

    def bind_endpoint_identity(
        self,
        endpoint_identity: Mapping[str, Any],
    ) -> None:
        value = dict(endpoint_identity)
        if self._bound_endpoint_identity is not None:
            raise SetupBlocked("publisher endpoint identity was already bound")
        if not value:
            raise SetupBlocked("publisher endpoint identity is empty")
        self._bound_endpoint_identity = value

    def _endpoint_identity(self) -> dict[str, Any]:
        if self._bound_endpoint_identity is None:
            raise SetupBlocked(
                "publisher endpoint identity was not bound before snapshot"
            )
        return dict(self._bound_endpoint_identity)

    def read_recovery_sentinel(self) -> dict[str, str]:
        rows = self.database.query(
            """
            SELECT
                sentinel_id,
                run_id,
                fault_id,
                source_sha
            FROM forwin_recovery_run_sentinel
            WHERE singleton = true
            """,
        )
        if len(rows) != 1:
            raise SetupBlocked(
                "database endpoint returned no unique recovery sentinel"
            )
        return {
            "table": "forwin_recovery_run_sentinel",
            **{
                key: str(rows[0].get(key) or "")
                for key in (
                    "sentinel_id",
                    "run_id",
                    "fault_id",
                    "source_sha",
                )
            },
        }

    def insert_fixture(self, fixture: PublisherFixture) -> None:
        if self._job_rows(fixture):
            raise SetupBlocked(
                "publisher fixture job identity or natural key already exists"
            )
        statement, parameters = fixture_insert(fixture)
        if self.database.execute(statement, parameters) != 1:
            raise SetupBlocked("publisher fixture INSERT did not affect one row")
        rows = self._job_rows(fixture)
        if len(rows) != 1:
            raise SetupBlocked(
                "publisher fixture INSERT did not create exactly one identity"
            )
        normalized = self._normalize_job(rows[0], fixture)
        if (
            normalized["job_id"] != fixture.job_id
            or normalized["logical_key"] != fixture.logical_key
        ):
            raise SetupBlocked("publisher fixture identity drifted after INSERT")

    def _job_rows(self, fixture: PublisherFixture) -> list[dict[str, Any]]:
        return self.database.query(
            """
            SELECT
                id AS job_id,
                idempotency_key AS logical_key,
                task_kind,
                project_id,
                canon_commit_id,
                candidate_id,
                chapter_number,
                platform_id,
                status,
                publish,
                book_name,
                chapter_title,
                body_text,
                body_sha256,
                upload_url,
                abort_requested,
                extension_client_id AS owner_token,
                extension_client_id,
                current_attempt_id,
                available_at,
                reconcile_after,
                claimed_at,
                started_at,
                finished_at,
                deleted_at,
                paused_at,
                pause_reason,
                current_url,
                result_message,
                error_message,
                result_payload_json,
                created_at,
                updated_at,
                CURRENT_TIMESTAMP AS database_now
            FROM publisher_upload_jobs
            WHERE id = %s OR idempotency_key = %s
            ORDER BY id
            """,
            (fixture.job_id, fixture.logical_key),
        )

    def _exact_row(self, fixture: PublisherFixture) -> dict[str, Any]:
        rows = self._job_rows(fixture)
        if len(rows) != 1:
            raise SetupBlocked(
                "publisher fixture identity has duplicate or missing natural keys"
            )
        row = rows[0]
        if (
            str(row.get("job_id") or "") != fixture.job_id
            or str(row.get("logical_key") or "") != fixture.logical_key
        ):
            raise SetupBlocked("publisher fixture job identity drifted")
        return row

    def owner_token(self, fixture: PublisherFixture) -> str:
        return str(self._exact_row(fixture).get("owner_token") or "")

    def wait_backend_owner(
        self,
        fixture: PublisherFixture,
        *,
        previous_owner_token: str = "",
        timeout_seconds: float = 300.0,
    ) -> str:
        deadline = self.monotonic() + timeout_seconds
        last = ""
        while self.monotonic() < deadline:
            row = self._exact_row(fixture)
            last = str(row.get("owner_token") or "")
            if (
                row.get("status") == "running"
                and last.startswith("backend:")
                and last != previous_owner_token
            ):
                return last
            self.sleep(0.5)
        raise SetupBlocked(
            "publisher backend did not durably claim the exact job with a "
            f"new owner token (last={last!r})"
        )

    def wait_status(
        self,
        fixture: PublisherFixture,
        expected: str,
        *,
        timeout_seconds: float = 300.0,
    ) -> None:
        deadline = self.monotonic() + timeout_seconds
        last = ""
        while self.monotonic() < deadline:
            last = str(self._exact_row(fixture).get("status") or "")
            if last == expected:
                return
            if last in {"failed", "cancelled"} and last != expected:
                break
            self.sleep(0.5)
        raise SetupBlocked(
            f"publisher job did not reach {expected}; observed {last or 'empty'}"
        )

    def _attempt_rows(self, fixture: PublisherFixture) -> list[dict[str, Any]]:
        return self.database.query(
            """
            SELECT
                id AS attempt_id,
                upload_job_id AS job_id,
                attempt_number,
                attempt_kind,
                worker_id AS owner_token,
                lease_epoch,
                status,
                phase,
                content_sha256,
                error_code,
                result_json
            FROM publisher_upload_attempts
            WHERE upload_job_id = %s
            ORDER BY attempt_number, id
            """,
            (fixture.job_id,),
        )

    def _attempts(self, fixture: PublisherFixture) -> list[dict[str, Any]]:
        return [
            {
                key: (
                    int(row.get(key) or 0)
                    if key in {"attempt_number", "lease_epoch"}
                    else str(row.get(key) or "")
                )
                for key in (
                    "attempt_id",
                    "job_id",
                    "attempt_number",
                    "attempt_kind",
                    "owner_token",
                    "lease_epoch",
                    "status",
                    "phase",
                    "content_sha256",
                    "error_code",
                )
            }
            for row in self._attempt_rows(fixture)
        ]

    def _receipts(self, fixture: PublisherFixture) -> list[dict[str, str]]:
        rows = self.database.query(
            """
            SELECT
                id AS receipt_id,
                upload_job_id AS job_id,
                upload_attempt_id AS attempt_id,
                receipt_key AS natural_key
            FROM publisher_upload_receipts
            WHERE upload_job_id = %s
            ORDER BY receipt_key, id
            """,
            (fixture.job_id,),
        )
        return [
            {
                key: str(row.get(key) or "")
                for key in (
                    "receipt_id",
                    "job_id",
                    "attempt_id",
                    "natural_key",
                )
            }
            for row in rows
        ]

    def _normalize_job(
        self,
        row: Mapping[str, Any],
        fixture: PublisherFixture,
        *,
        risk: bool = False,
    ) -> dict[str, Any]:
        payload = _json_object(row.get("result_payload_json"))
        normalized: dict[str, Any] = {
            "job_id": str(row.get("job_id") or ""),
            "logical_key": str(row.get("logical_key") or ""),
            "task_kind": str(row.get("task_kind") or ""),
            "project_id": str(row.get("project_id") or ""),
            "platform_id": str(row.get("platform_id") or ""),
            "status": str(row.get("status") or ""),
            "publish": bool(row.get("publish")),
            "book_name": str(row.get("book_name") or ""),
            "chapter_title": str(row.get("chapter_title") or ""),
            "body_sha256": str(row.get("body_sha256") or ""),
            "unsafe_payload_paths": _unsafe_payload_paths(payload),
        }
        if fixture.task_kind == "cover_generate":
            normalized.update(
                owner_token=str(row.get("owner_token") or ""),
                artifact_id=str(
                    payload.get("selected_cover_asset_id") or ""
                ),
            )
        elif risk:
            pause = payload.get("risk_pause")
            resume = payload.get("risk_resume")
            pause = pause if isinstance(pause, Mapping) else {}
            resume = resume if isinstance(resume, Mapping) else {}
            boundary = ""
            evidence = pause.get("evidence")
            if isinstance(evidence, Mapping):
                boundary = str(evidence.get("boundary") or "")
            if not boundary:
                for attempt in self._attempt_rows(fixture):
                    result = _optional_json_object(attempt.get("result_json"))
                    attempt_evidence = result.get("evidence")
                    if isinstance(attempt_evidence, Mapping):
                        boundary = str(
                            attempt_evidence.get("boundary") or ""
                        )
                        if boundary:
                            break
            normalized.update(
                pause_reason=str(row.get("pause_reason") or ""),
                pause_token=str(
                    pause.get("pause_token")
                    or resume.get("pause_token")
                    or ""
                ),
                risk_boundary=boundary,
            )
        return normalized

    @staticmethod
    def _timestamp(value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError as exc:
                raise SetupBlocked(
                    "publisher job timestamp is malformed"
                ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()

    def _canonical_job(self, fixture: PublisherFixture) -> dict[str, Any]:
        row = self._exact_row(fixture)
        payload = _json_object(row.get("result_payload_json"))
        pause = payload.get("risk_pause")
        resume = payload.get("risk_resume")
        pause = pause if isinstance(pause, Mapping) else {}
        resume = resume if isinstance(resume, Mapping) else {}
        boundary = ""
        evidence = pause.get("evidence")
        if isinstance(evidence, Mapping):
            boundary = str(evidence.get("boundary") or "")
        if not boundary:
            for attempt in self._attempt_rows(fixture):
                result = _optional_json_object(attempt.get("result_json"))
                attempt_evidence = result.get("evidence")
                if isinstance(attempt_evidence, Mapping):
                    boundary = str(attempt_evidence.get("boundary") or "")
                    if boundary:
                        break
        return {
            "job_id": str(row.get("job_id") or ""),
            "logical_key": str(row.get("logical_key") or ""),
            "task_kind": str(row.get("task_kind") or ""),
            "project_id": str(row.get("project_id") or ""),
            "platform_id": str(row.get("platform_id") or ""),
            "status": str(row.get("status") or ""),
            "publish": bool(row.get("publish")),
            "book_name": str(row.get("book_name") or ""),
            "chapter_title": str(row.get("chapter_title") or ""),
            "body_sha256": str(row.get("body_sha256") or ""),
            "unsafe_payload_paths": _unsafe_payload_paths(payload),
            "canon_commit_id": str(row.get("canon_commit_id") or ""),
            "candidate_id": str(row.get("candidate_id") or ""),
            "chapter_number": int(row.get("chapter_number") or 0),
            "body_text": str(row.get("body_text") or ""),
            "upload_url": str(row.get("upload_url") or ""),
            "abort_requested": bool(row.get("abort_requested")),
            "owner_token": str(row.get("owner_token") or ""),
            "extension_client_id": str(
                row.get("extension_client_id") or ""
            ),
            "current_attempt_id": str(
                row.get("current_attempt_id") or ""
            ),
            "available_at": self._timestamp(row.get("available_at")),
            "reconcile_after": self._timestamp(row.get("reconcile_after")),
            "claimed_at": self._timestamp(row.get("claimed_at")),
            "started_at": self._timestamp(row.get("started_at")),
            "finished_at": self._timestamp(row.get("finished_at")),
            "deleted_at": self._timestamp(row.get("deleted_at")),
            "paused_at": self._timestamp(row.get("paused_at")),
            "pause_reason": str(row.get("pause_reason") or ""),
            "pause_token": str(
                pause.get("pause_token")
                or resume.get("pause_token")
                or ""
            ),
            "risk_boundary": boundary,
            "current_url": str(row.get("current_url") or ""),
            "result_message": str(row.get("result_message") or ""),
            "error_message": str(row.get("error_message") or ""),
            "result_payload": payload,
            "created_at": self._timestamp(row.get("created_at")),
            "updated_at": self._timestamp(row.get("updated_at")),
            "database_now": self._timestamp(row.get("database_now")),
        }

    def _job(self, fixture: PublisherFixture, *, risk: bool = False) -> dict[str, Any]:
        return self._normalize_job(
            self._exact_row(fixture),
            fixture,
            risk=risk,
        )

    def _jobs(self, fixture: PublisherFixture) -> list[dict[str, str]]:
        return [
            {
                "job_id": str(row.get("job_id") or ""),
                "logical_key": str(row.get("logical_key") or ""),
                "task_kind": str(row.get("task_kind") or ""),
            }
            for row in self._job_rows(fixture)
        ]

    def _cover_assets(self, fixture: PublisherFixture) -> list[dict[str, Any]]:
        row = self._exact_row(fixture)
        payload = _json_object(row.get("result_payload_json"))
        asset_ids = [
            str(value)
            for value in payload.get("cover_asset_ids") or []
            if str(value or "")
        ]
        if not asset_ids:
            return []
        rows = self.database.query(
            """
            SELECT
                id AS asset_id,
                file_path,
                file_size_bytes AS file_size,
                mime_type
            FROM publisher_cover_assets
            WHERE id = ANY(%s)
            ORDER BY id
            """,
            (asset_ids,),
        )
        return [
            {
                "asset_id": str(item.get("asset_id") or ""),
                "job_id": fixture.job_id,
                "file_path": str(item.get("file_path") or ""),
                "file_size": int(item.get("file_size") or 0),
                "mime_type": str(item.get("mime_type") or ""),
            }
            for item in rows
        ]

    def _resume_actions(self, fixture: PublisherFixture) -> list[dict[str, str]]:
        rows = self.database.query(
            """
            SELECT
                id AS action_id,
                upload_job_id AS job_id,
                action,
                pause_token,
                actor_id,
                auth_method,
                reason,
                old_state_json,
                new_state_json
            FROM publisher_operator_actions
            WHERE upload_job_id = %s
              AND action = 'resume'
            ORDER BY created_at, id
            """,
            (fixture.job_id,),
        )
        actions: list[dict[str, str]] = []
        for row in rows:
            old_state = _optional_json_object(row.get("old_state_json"))
            new_state = _optional_json_object(row.get("new_state_json"))
            pause_token = str(row.get("pause_token") or "")
            actions.append(
                {
                    "action_id": str(row.get("action_id") or ""),
                    "job_id": str(row.get("job_id") or ""),
                    "natural_key": (
                        f"{row.get('job_id')}:resume:{pause_token}"
                    ),
                    "action": str(row.get("action") or ""),
                    "pause_token": pause_token,
                    "actor_id": str(row.get("actor_id") or ""),
                    "auth_method": str(row.get("auth_method") or ""),
                    "reason_sha256": hashlib.sha256(
                        str(row.get("reason") or "").encode("utf-8")
                    ).hexdigest(),
                    "old_status": str(old_state.get("status") or ""),
                    "new_status": str(new_state.get("status") or ""),
                }
            )
        return actions

    def backend_snapshot(
        self,
        *,
        source_sha: str,
        fault_id: str,
        stage: str,
        fixture: PublisherFixture,
        cover_files: list[dict[str, Any]] | None = None,
        stale_observation: Mapping[str, Any] | None = None,
        terminal_writes: list[dict[str, Any]] | None = None,
        residue: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        snapshot = _snapshot_base(
            source_sha=source_sha,
            fault_kind=fixture.fault_kind,
            fault_id=fault_id,
            stage=stage,
            fixture=fixture,
            endpoint_identity=self._endpoint_identity(),
        )
        database = snapshot["state"]["database"]
        database.update(
            job=self._job(fixture),
            jobs=self._jobs(fixture),
            attempts=self._attempts(fixture),
            receipts=self._receipts(fixture),
        )
        if stage in {"during", "after"}:
            database["cover_assets"] = self._cover_assets(fixture)
            snapshot["state"]["external"]["cover_files"] = list(
                cover_files or []
            )
        if stage == "after":
            snapshot["state"]["api"]["stale_token_observation"] = dict(
                stale_observation or {}
            )
            snapshot["state"]["barrier"].update(
                terminal_writes=list(terminal_writes or []),
                residue=dict(residue or {}),
            )
        return snapshot

    def browser_snapshot(
        self,
        *,
        source_sha: str,
        fault_id: str,
        stage: str,
        fixture: PublisherFixture,
        heartbeat: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = _snapshot_base(
            source_sha=source_sha,
            fault_kind=fixture.fault_kind,
            fault_id=fault_id,
            stage=stage,
            fixture=fixture,
            endpoint_identity=self._endpoint_identity(),
        )
        snapshot["state"]["database"].update(
            job=self._canonical_job(fixture),
            attempts=self._attempts(fixture),
            receipts=self._receipts(fixture),
        )
        snapshot["state"]["external"]["browser_heartbeat"] = {
            "observation_id": (
                f"publisher-heartbeat-{stage}-"
                + hashlib.sha256(
                    f"{fault_id}:{heartbeat.get('browser_id')}:{stage}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:20]
            ),
            **dict(heartbeat),
        }
        return snapshot

    def risk_snapshot(
        self,
        *,
        source_sha: str,
        fault_id: str,
        stage: str,
        fixture: PublisherFixture,
        replay: Mapping[str, Any] | None = None,
        pre_discard_state: Mapping[str, Any] | None = None,
        browser_hold_terminal: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = _snapshot_base(
            source_sha=source_sha,
            fault_kind=fixture.fault_kind,
            fault_id=fault_id,
            stage=stage,
            fixture=fixture,
            endpoint_identity=self._endpoint_identity(),
        )
        snapshot["state"]["database"].update(
            job=self._canonical_job(fixture),
            attempts=self._attempts(fixture),
        )
        if stage == "after":
            actions = self._resume_actions(fixture)
            snapshot["state"]["database"].update(
                receipts=self._receipts(fixture),
                resume_actions=actions,
            )
            pre_discard = dict(pre_discard_state or {})
            snapshot["state"]["database"].update(
                pre_discard_job=dict(
                    pre_discard.get("job") or {}
                ),
                pre_discard_attempts=list(
                    pre_discard.get("attempts") or []
                ),
                pre_discard_receipts=list(
                    pre_discard.get("receipts") or []
                ),
                pre_discard_resume_actions=list(
                    pre_discard.get("resume_actions") or []
                ),
            )
            terminal = dict(browser_hold_terminal or {})
            after_state = terminal.get("after")
            after_state = (
                dict(after_state)
                if isinstance(after_state, Mapping)
                else {}
            )
            snapshot["state"]["external"]["browser_hold_terminal"] = {
                "action": str(terminal.get("action") or ""),
                "fault_id": str(terminal.get("fault_id") or ""),
                "hold_id": str(terminal.get("hold_id") or ""),
                "service": str(terminal.get("service") or ""),
                "container_id": str(
                    after_state.get("container_id") or ""
                ),
                "image_id": str(after_state.get("image_id") or ""),
                "exists": bool(after_state.get("exists")),
                "running": bool(after_state.get("running")),
            }
            replay_payload = dict(replay or {})
            replay_payload.update(
                observation_id=(
                    "publisher-resume-replay-"
                    + hashlib.sha256(
                        f"{fault_id}:{fixture.job_id}".encode("utf-8")
                    ).hexdigest()[:20]
                ),
                job_id=fixture.job_id,
                action_id=(
                    actions[0]["action_id"] if len(actions) == 1 else ""
                ),
            )
            snapshot["state"]["api"]["resume_replay"] = replay_payload
        return snapshot

    def risk_terminal_state(
        self,
        fixture: PublisherFixture,
    ) -> dict[str, Any]:
        return {
            "job": self._canonical_job(fixture),
            "attempts": self._attempts(fixture),
            "receipts": self._receipts(fixture),
            "resume_actions": self._resume_actions(fixture),
        }


def normalize_cover_inventory(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    if set(payload) != {"root", "root_exists", "files"}:
        raise SetupBlocked("publisher cover inventory field set drifted")
    if payload.get("root") != PUBLISHER_COVER_ROOT:
        raise SetupBlocked("publisher cover inventory root drifted")
    if payload.get("root_exists") is not True:
        raise SetupBlocked("publisher cover inventory root does not exist")
    rows = payload.get("files")
    if not isinstance(rows, list):
        raise SetupBlocked("publisher cover inventory is malformed")
    normalized: list[dict[str, Any]] = []
    observed_paths: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"path", "size", "content_sha256"}
        ):
            raise SetupBlocked("publisher cover inventory row is malformed")
        raw_path = str(row.get("path") or "")
        relative = PurePosixPath(raw_path)
        if (
            not raw_path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != raw_path
            or "\\" in raw_path
        ):
            raise SetupBlocked("publisher cover inventory path is unsafe")
        digest = str(row.get("content_sha256") or "")
        raw_size = row.get("size")
        if type(raw_size) is not int:
            raise SetupBlocked("publisher cover inventory metadata is invalid")
        size = raw_size
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None or size < 0:
            raise SetupBlocked("publisher cover inventory metadata is invalid")
        if raw_path in observed_paths:
            raise SetupBlocked("publisher cover inventory path is duplicated")
        observed_paths.add(raw_path)
        normalized.append(
            {
                "path": raw_path,
                "size": size,
                "content_sha256": digest,
            }
        )
    return sorted(normalized, key=lambda item: item["path"])


@dataclass(frozen=True, slots=True)
class BarrierNames:
    scope_table: str
    function: str
    trigger: str


def barrier_names(fault_id: str) -> BarrierNames:
    suffix = hashlib.sha256(
        f"publisher-terminal:{validate_fault_id(fault_id)}".encode("ascii")
    ).hexdigest()[:20]
    return BarrierNames(
        scope_table=f"recovery_publisher_scope_{suffix}",
        function=f"recovery_publisher_terminal_{suffix}",
        trigger=f"recovery_publisher_terminal_{suffix}",
    )


def advisory_key(fault_id: str) -> int:
    unsigned = int.from_bytes(
        hashlib.sha256(
            f"publisher-terminal-advisory:{validate_fault_id(fault_id)}".encode(
                "ascii"
            )
        ).digest()[:8],
        "big",
        signed=False,
    )
    value = unsigned if unsigned < 2**63 else unsigned - 2**64
    return value or 1


def advisory_lock_identity(key: int) -> tuple[int, int]:
    unsigned = key & ((1 << 64) - 1)
    return ((unsigned >> 32) & 0xFFFFFFFF, unsigned & 0xFFFFFFFF)


class TerminalWriteBarrier:
    target_table = "publisher_upload_jobs"

    def __init__(
        self,
        *,
        fault_id: str,
        database_url: str,
        connect: Callable[[str], Any] = psycopg_connect,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fault_id = validate_fault_id(fault_id)
        self.database_url = normalize_database_url(database_url)
        self.connect = connect
        self.sleep = sleep
        self.monotonic = monotonic
        self.names = barrier_names(fault_id)
        self.advisory_key = advisory_key(fault_id)
        self.lock_identity = advisory_lock_identity(self.advisory_key)
        self.holder_application_name = (
            f"{self.names.scope_table[:48]}_holder"
        )
        self._admin: Any | None = None
        self._holder: Any | None = None
        self.job_id = ""
        self.observations: list[dict[str, Any]] = []
        self.last_residue: dict[str, int] | None = None

    def install(self, *, job_id: str) -> None:
        if not str(job_id or ""):
            raise SetupBlocked("publisher terminal barrier job identity is empty")
        if self._admin is not None or self._holder is not None:
            raise RunnerError("publisher terminal barrier is already installed")
        self.job_id = str(job_id)
        try:
            self._admin = self.connect(self.database_url)
            self._admin.autocommit = True
            self._holder = self.connect(self.database_url)
            self._holder.autocommit = True
            if sum(self._residue().values()):
                raise SetupBlocked(
                    "fault-scoped publisher terminal barrier residue exists"
                )
            with self._admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "CREATE UNLOGGED TABLE {} ("
                        "job_id text PRIMARY KEY, advisory_key bigint NOT NULL)"
                    ).format(sql.Identifier(self.names.scope_table))
                )
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {} (job_id, advisory_key) VALUES (%s, %s)"
                    ).format(sql.Identifier(self.names.scope_table)),
                    (self.job_id, self.advisory_key),
                )
                cursor.execute(self._function_statement())
                cursor.execute(
                    sql.SQL(
                        "CREATE TRIGGER {} BEFORE UPDATE OF status ON {} "
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
        return sql.SQL(
            "CREATE FUNCTION {}() RETURNS trigger "
            "LANGUAGE plpgsql AS $forwin_recovery$ "
            "DECLARE scoped_key bigint; "
            "BEGIN "
            "IF NEW.status IN ('succeeded', 'failed', 'cancelled') "
            "AND NEW.status IS DISTINCT FROM OLD.status "
            "AND NEW.extension_client_id <> '' THEN "
            "SELECT advisory_key INTO scoped_key FROM {} "
            "WHERE job_id = NEW.id; "
            "IF scoped_key IS NOT NULL THEN "
            "PERFORM pg_advisory_xact_lock(scoped_key); "
            "END IF; "
            "END IF; "
            "RETURN NEW; "
            "END "
            "$forwin_recovery$"
        ).format(
            sql.Identifier(self.names.function),
            sql.Identifier(self.names.scope_table),
        )

    def _lock_rows(self) -> list[dict[str, Any]]:
        if self._admin is None:
            raise RunnerError("publisher terminal barrier is not installed")
        with self._admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    locks.pid,
                    locks.granted,
                    activity.application_name,
                    activity.query,
                    activity.wait_event_type,
                    pg_blocking_pids(locks.pid) AS blocking_pids
                FROM pg_locks AS locks
                JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
                WHERE locks.locktype = 'advisory'
                  AND locks.classid::bigint = %s
                  AND locks.objid::bigint = %s
                ORDER BY locks.granted DESC, locks.pid
                """,
                self.lock_identity,
            )
            return [dict(row) for row in cursor.fetchall()]

    def wait_for_blocked_terminal(
        self,
        *,
        expected_owner_token: str,
        owner_reader: Callable[[], str],
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        deadline = self.monotonic() + timeout_seconds
        last_error: BaseException | None = None
        while self.monotonic() < deadline:
            try:
                observation = terminal_barrier_observation(
                    barrier=self,
                    rows=self._lock_rows(),
                    job_id=self.job_id,
                    expected_owner_token=expected_owner_token,
                    current_owner_token=owner_reader(),
                )
                payload = {
                    "observation_id": (
                        f"publisher-terminal-{len(self.observations) + 1}-"
                        + hashlib.sha256(
                            f"{self.fault_id}:{expected_owner_token}".encode(
                                "utf-8"
                            )
                        ).hexdigest()[:20]
                    ),
                    "job_id": self.job_id,
                    "owner_token": observation.owner_token,
                    "holder_pid": observation.holder_pid,
                    "waiter_pid": observation.waiter_pid,
                    "waiter_application_name": (
                        observation.waiter_application_name
                    ),
                    "waiter_role": "forwin",
                    "blocking_pids": [observation.holder_pid],
                    "trigger_name": self.names.trigger,
                    "function_name": self.names.function,
                    "scope_table": self.names.scope_table,
                    "advisory_key": self.advisory_key,
                }
                self.observations.append(payload)
                return payload
            except SetupBlocked as exc:
                last_error = exc
                self.sleep(0.5)
        raise SetupBlocked(
            "publisher terminal-write barrier did not produce the exact "
            f"blocked waiter: {last_error or 'timeout'}"
        )

    def _residue(self) -> dict[str, int]:
        if self._admin is None:
            return {
                "trigger_count": 0,
                "function_count": 0,
                "scope_table_count": 0,
                "advisory_lock_count": 0,
            }
        with self._admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM pg_trigger
                     WHERE tgname = %s AND NOT tgisinternal)::integer
                        AS trigger_count,
                    (SELECT count(*) FROM pg_proc
                     WHERE proname = %s)::integer AS function_count,
                    (SELECT count(*) FROM pg_class
                     WHERE relname = %s)::integer AS scope_table_count,
                    (SELECT count(*) FROM pg_locks
                     WHERE locktype = 'advisory'
                       AND classid::bigint = %s
                       AND objid::bigint = %s)::integer
                        AS advisory_lock_count
                """,
                (
                    self.names.trigger,
                    self.names.function,
                    self.names.scope_table,
                    *self.lock_identity,
                ),
            )
            row = cursor.fetchone() or {}
        return {
            key: int(row.get(key) or 0)
            for key in (
                "trigger_count",
                "function_count",
                "scope_table_count",
                "advisory_lock_count",
            )
        }

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
                        errors.append("scoped publisher advisory lock was not held")
            except BaseException as exc:
                errors.append(f"publisher advisory unlock: {exc}")
            finally:
                try:
                    self._holder.close()
                except BaseException as exc:
                    errors.append(f"publisher barrier holder close: {exc}")
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
                self.last_residue = self._residue()
                if sum(self.last_residue.values()):
                    errors.append(
                        "publisher terminal barrier cleanup residue: "
                        + json.dumps(self.last_residue, sort_keys=True)
                    )
            except BaseException as exc:
                errors.append(f"publisher barrier object cleanup: {exc}")
            finally:
                try:
                    self._admin.close()
                except BaseException as exc:
                    errors.append(f"publisher barrier admin close: {exc}")
                self._admin = None
        if self.last_residue is None:
            self.last_residue = {
                "trigger_count": 0,
                "function_count": 0,
                "scope_table_count": 0,
                "advisory_lock_count": 0,
            }
        if errors:
            raise RunnerError("; ".join(errors))


def production_stale_token_probe(
    *,
    database_url: str,
    job_id: str,
    stale_owner_token: str,
    current_owner_token: str,
) -> dict[str, Any]:
    from forwin.models.base import get_engine, get_session_factory
    from forwin.publisher_runtime.covers import PublisherCoverService

    engine = get_engine(database_url)
    try:
        service = PublisherCoverService(
            session_factory=get_session_factory(engine),
            cover_dir=PUBLISHER_COVER_ROOT,
        )
        response = service.generate_for_job(
            job_id,
            owner_token=stale_owner_token,
        )
    finally:
        engine.dispose()
    return validate_stale_token_response(
        job_id=job_id,
        stale_owner_token=stale_owner_token,
        current_owner_token=current_owner_token,
        response=response,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _recovery_client_id(fault_id: str) -> str:
    return (
        "publisher-recovery-client-"
        + hashlib.sha256(fault_id.encode("ascii")).hexdigest()[:24]
    )


def _failure_text(error: BaseException) -> str:
    return (
        " ".join(
            (str(error) or type(error).__name__)
            .replace("\x00", " ")
            .split()
        )[:512]
        or "Task 6 setup blocked"
    )


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
        database_url: str,
        mcp_url: str,
        api_url: str,
        controller: Any,
        sql_collector: SQLCollector,
        api: PublisherAPI,
        writer: Any,
        barrier_factory: Callable[[], Any] | None = None,
        stale_probe: Callable[..., dict[str, Any]] = (
            production_stale_token_probe
        ),
    ) -> None:
        if fault_kind not in SUPPORTED_FAULTS:
            raise RunnerError(f"unsupported Task 6 fault: {fault_kind}")
        self.fault_kind = fault_kind
        self.fault_id = validate_fault_id(fault_id)
        self.source_sha = source_sha
        self.database_url = database_url
        self.mcp_url = mcp_url
        self.api_url = api_url
        self.controller = controller
        self.sql = sql_collector
        self.api = api
        self.writer = writer
        self.barrier_factory = barrier_factory
        self.stale_probe = stale_probe
        self.fixture = publisher_fixture(fault_kind, fault_id)
        self.barrier: Any | None = None
        self.stage = "initial"
        self.stack_started = False
        self.primary_faulted = False
        self.recovered = False
        self.setup_holds: list[tuple[str, str]] = []

    def run(self) -> LiveRunResult:
        snapshots: dict[str, dict[str, Any]] | None = None
        failure: BaseException | None = None
        cleanup_errors: list[str] = []
        try:
            self.stage = "fresh_up"
            self.stack_started = True
            self.controller.fresh_up(self.fault_id)
            self.stage = "endpoint_binding"
            require_client_endpoint(
                self.api,
                attribute="api_url",
                expected_url=self.api_url,
                label="Publisher API",
            )
            endpoint_identity = bind_recovery_endpoints(
                controller=self.controller,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                api_url=self.api_url,
                mcp_url=self.mcp_url,
                database_url=self.database_url,
                sentinel_reader=self.sql.read_recovery_sentinel,
            )
            self.sql.bind_endpoint_identity(endpoint_identity)
            if self.fault_kind == "publisher_backend_unavailable":
                snapshots = self._run_backend()
            elif self.fault_kind == "publisher_browser_unavailable":
                snapshots = self._run_browser()
            else:
                snapshots = self._run_risk()
        except BaseException as exc:
            failure = exc
        if self.barrier is not None:
            try:
                self.barrier.cleanup()
            except BaseException as exc:
                cleanup_errors.append(f"terminal barrier cleanup: {exc}")
                if failure is None or not isinstance(exc, Exception):
                    failure = exc
            self.barrier = None
        if self.setup_holds:
            if failure is None:
                failure = RunnerError(
                    "runner completed with an active auxiliary setup hold"
                )
            for service, hold_id in reversed(self.setup_holds[:]):
                try:
                    self.controller.setup_discard(
                        service,
                        self.fault_id,
                        hold_id,
                    )
                except BaseException as exc:
                    cleanup_errors.append(
                        f"setup hold discard {service}/{hold_id}: {exc}"
                    )
                    if not isinstance(exc, Exception):
                        failure = exc
                else:
                    self.setup_holds.remove((service, hold_id))
        if failure is not None and not isinstance(failure, Exception):
            if self.stack_started:
                try:
                    self.controller.interrupt_cleanup(self.fault_id)
                except BaseException as exc:
                    cleanup_errors.append(f"interrupt cleanup: {exc}")
            raise failure.with_traceback(failure.__traceback__)
        if failure is not None:
            if self.stack_started:
                try:
                    self.controller.abort(
                        self.fault_id,
                        self.stage,
                        _failure_text(failure),
                    )
                except BaseException as exc:
                    cleanup_errors.append(f"controller abort: {exc}")
                    if not isinstance(exc, Exception):
                        try:
                            self.controller.interrupt_cleanup(self.fault_id)
                        except BaseException as cleanup_exc:
                            cleanup_errors.append(
                                f"interrupt cleanup: {cleanup_exc}"
                            )
                        raise exc.with_traceback(exc.__traceback__)
            report = self.writer.write_setup_blocked(
                fault_kind=self.fault_kind,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                failure_stage=self.stage,
                failure_reason=_failure_text(failure),
                cleanup_errors=cleanup_errors,
            )
            return LiveRunResult("setup_blocked", report)
        if snapshots is None:
            raise RunnerError("Task 6 runner produced no snapshots")
        self.stage = "terminal_destroy"
        try:
            self.controller.destroy()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                try:
                    self.controller.interrupt_cleanup(self.fault_id)
                except BaseException:
                    pass
                raise
            cleanup_errors.append(f"publisher stack destroy: {exc}")
            try:
                self.controller.abort(
                    self.fault_id,
                    self.stage,
                    _failure_text(exc),
                )
            except BaseException as abort_exc:
                cleanup_errors.append(f"controller abort: {abort_exc}")
            report = self.writer.write_setup_blocked(
                fault_kind=self.fault_kind,
                fault_id=self.fault_id,
                source_sha=self.source_sha,
                failure_stage=self.stage,
                failure_reason=_failure_text(exc),
                cleanup_errors=cleanup_errors,
            )
            return LiveRunResult("setup_blocked", report)
        self.stage = "evidence_write"
        report = self.writer.write_pass_report(
            fault_kind=self.fault_kind,
            fault_id=self.fault_id,
            source_sha=self.source_sha,
            snapshots=snapshots,
            event_log_path=self.controller.event_log_path,
            supplemental_artifacts={},
        )
        return LiveRunResult("pass", report)

    def _hold(
        self,
        service: str,
        hold_id: str,
        *,
        purpose: str = "auxiliary",
    ) -> None:
        self.controller.setup_hold(
            service,
            self.fault_id,
            hold_id,
            fault_kind=self.fault_kind,
            purpose=purpose,
        )
        self.setup_holds.append((service, hold_id))

    def _release_hold(self, service: str, hold_id: str) -> None:
        self.controller.setup_release(service, self.fault_id, hold_id)
        self.setup_holds.remove((service, hold_id))

    def _discard_hold(self, service: str, hold_id: str) -> dict[str, Any]:
        event = self.controller.setup_discard(
            service,
            self.fault_id,
            hold_id,
        )
        self.setup_holds.remove((service, hold_id))
        return event

    def _cover_inventory(self) -> list[dict[str, Any]]:
        return normalize_cover_inventory(
            self.controller.file_inventory(
                "publisher-browser",
                self.fault_id,
                PUBLISHER_COVER_ROOT,
            )
        )

    def _run_backend(self) -> dict[str, dict[str, Any]]:
        if self.barrier_factory is None:
            raise SetupBlocked("publisher terminal barrier factory is missing")
        hold_id = f"backend-fixture-{self.fault_id}"[:128]
        self.stage = "backend_setup_hold"
        self._hold(
            "publisher-worker",
            hold_id,
            purpose="pre-fault-boundary",
        )
        self.stage = "backend_fixture_insert"
        self.sql.insert_fixture(self.fixture)
        self.stage = "terminal_barrier_install"
        self.barrier = self.barrier_factory()
        self.barrier.install(job_id=self.fixture.job_id)
        self.stage = "backend_first_start"
        self._release_hold("publisher-worker", hold_id)
        old_owner = self.sql.wait_backend_owner(self.fixture)
        self.stage = "terminal_barrier_old_owner"
        self.barrier.wait_for_blocked_terminal(
            expected_owner_token=old_owner,
            owner_reader=lambda: self.sql.owner_token(self.fixture),
        )
        before = self.sql.backend_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="before",
            fixture=self.fixture,
        )
        self.stage = "publisher_worker_sigkill"
        self.controller.kill("publisher-worker", self.fault_id)
        self.primary_faulted = True
        during_files = self._cover_inventory()
        during = self.sql.backend_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="during",
            fixture=self.fixture,
            cover_files=during_files,
        )
        self.stage = "publisher_worker_recovery"
        self.controller.start("publisher-worker", self.fault_id)
        self.recovered = True
        new_owner = self.sql.wait_backend_owner(
            self.fixture,
            previous_owner_token=old_owner,
        )
        self.stage = "terminal_barrier_new_owner"
        self.barrier.wait_for_blocked_terminal(
            expected_owner_token=new_owner,
            owner_reader=lambda: self.sql.owner_token(self.fixture),
        )
        self.stage = "stale_owner_probe"
        stale = self.stale_probe(
            database_url=self.database_url,
            job_id=self.fixture.job_id,
            stale_owner_token=old_owner,
            current_owner_token=new_owner,
        )
        terminal_writes = list(self.barrier.observations)
        self.stage = "terminal_barrier_release"
        self.barrier.cleanup()
        residue = dict(self.barrier.last_residue or {})
        self.barrier = None
        self.stage = "backend_convergence"
        self.sql.wait_status(self.fixture, "succeeded")
        after_files = self._cover_inventory()
        after = self.sql.backend_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="after",
            fixture=self.fixture,
            cover_files=after_files,
            stale_observation=stale,
            terminal_writes=terminal_writes,
            residue=residue,
        )
        return {"before": before, "during": during, "after": after}

    def _run_browser(self) -> dict[str, dict[str, Any]]:
        self.stage = "browser_fixture_insert"
        self.sql.insert_fixture(self.fixture)
        self.stage = "browser_healthy_before"
        healthy_before = self.api.wait_heartbeat("healthy")
        browser_id = healthy_before["browser_id"]
        before = self.sql.browser_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="before",
            fixture=self.fixture,
            heartbeat=healthy_before,
        )
        self.stage = "publisher_browser_stop"
        self.controller.stop("publisher-browser", self.fault_id)
        self.primary_faulted = True
        stale = self.api.wait_heartbeat("stale", client_id=browser_id)
        during = self.sql.browser_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="during",
            fixture=self.fixture,
            heartbeat=stale,
        )
        self.stage = "publisher_browser_start"
        self.controller.start("publisher-browser", self.fault_id)
        self.recovered = True
        healthy_after = self.api.wait_heartbeat(
            "healthy",
            client_id=browser_id,
        )
        after = self.sql.browser_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="after",
            fixture=self.fixture,
            heartbeat=healthy_after,
        )
        return {"before": before, "during": during, "after": after}

    def _run_risk(self) -> dict[str, dict[str, Any]]:
        self.stage = "risk_browser_identity"
        heartbeat = self.api.wait_heartbeat("healthy")
        client_id = heartbeat["browser_id"]
        hold_id = f"risk-fixture-{self.fault_id}"[:128]
        self.stage = "risk_browser_setup_hold"
        self._hold("publisher-browser", hold_id)
        self.stage = "risk_fixture_insert"
        self.sql.insert_fixture(self.fixture)
        self.stage = "extension_claim"
        claim = self.api.claim(self.fixture, client_id)
        before = self.sql.risk_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="before",
            fixture=self.fixture,
        )
        risk_reason = RISK_REASONS[self.fault_kind]
        self.stage = "extension_typed_pause"
        pause = self.api.pause(
            self.fixture,
            claim,
            risk_reason=risk_reason,
            observed_at=_utc_now(),
        )
        self.stage = "typed_fault_mark"
        self.controller.mark(self.fault_kind, "fault", self.fault_id)
        self.primary_faulted = True
        during = self.sql.risk_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="during",
            fixture=self.fixture,
        )
        self.stage = "authenticated_operator_resume"
        replay = self.api.resume_twice(
            self.fixture,
            pause_token=str(pause["pause_token"]),
            risk_reason=risk_reason,
        )
        pre_discard_state = self.sql.risk_terminal_state(self.fixture)
        self.stage = "typed_recovery_mark"
        self.controller.mark(self.fault_kind, "recovery", self.fault_id)
        self.recovered = True
        self.stage = "risk_browser_setup_discard"
        browser_hold_terminal = self._discard_hold(
            "publisher-browser",
            hold_id,
        )
        self.stage = "risk_after_snapshot"
        after = self.sql.risk_snapshot(
            source_sha=self.source_sha,
            fault_id=self.fault_id,
            stage="after",
            fixture=self.fixture,
            replay=replay,
            pre_discard_state=pre_discard_state,
            browser_hold_terminal=browser_hold_terminal,
        )
        return {"before": before, "during": during, "after": after}

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
    extension_key: str
    operator_username: str
    operator_password: str


def _required_environment(environ: Mapping[str, str], name: str) -> str:
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
        raise RunnerError(f"unsupported Task 6 fault: {fault_kind}")
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
    return RunConfig(
        fault_kind=fault_kind,
        fault_id=fault_id,
        candidate_manifest=identity.manifest_path,
        source_sha=identity.source_sha,
        mcp_url=required_url(args.mcp_url, "MCP URL"),
        api_url=required_url(args.api_url, "API URL"),
        database_url=database_url,
        evidence_dir=evidence_dir,
        extension_key=_required_environment(
            environ,
            "FORWIN_PUBLISHER_EXTENSION_API_KEY",
        ),
        operator_username=_required_environment(
            environ,
            "FORWIN_HTTP_BASIC_USER",
        ),
        operator_password=_required_environment(
            environ,
            "FORWIN_HTTP_BASIC_PASSWORD",
        ),
    )


def build_live_runner(config: RunConfig) -> LiveRunner:
    controller = RecoveryController(
        candidate_manifest=config.candidate_manifest,
        evidence_dir=config.evidence_dir,
    )
    collector = SQLCollector(PsycopgDatabase(config.database_url))
    return LiveRunner(
        fault_kind=config.fault_kind,
        fault_id=config.fault_id,
        source_sha=config.source_sha,
        database_url=config.database_url,
        mcp_url=config.mcp_url,
        api_url=config.api_url,
        controller=controller,
        sql_collector=collector,
        api=PublisherAPI(
            api_url=config.api_url,
            extension_key=config.extension_key,
            operator_username=config.operator_username,
            operator_password=config.operator_password,
        ),
        writer=EvidenceWriter(evidence_dir=config.evidence_dir),
        barrier_factory=(
            (
                lambda: TerminalWriteBarrier(
                    fault_id=config.fault_id,
                    database_url=config.database_url,
                )
            )
            if config.fault_kind == "publisher_backend_unavailable"
            else None
        ),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run fault-local publisher recovery proofs."
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
