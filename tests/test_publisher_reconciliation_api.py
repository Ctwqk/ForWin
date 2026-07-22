from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from forwin.api_schema import (
    ExtensionClaimUploadJobRequest,
    UploadAttemptHeartbeatRequest,
    UploadAttemptPhaseRequest,
    UploadAttemptReceiptRequest,
    UploadAttemptReconcileRequest,
    UploadAttemptResultRequest,
)
from forwin.application.publisher import PublisherApplicationService
from forwin.http.adapters.api_publisher_routes import build_handlers
from forwin.http import HttpRuntime, create_app
from forwin.publisher_runtime.attempts import PublisherAttemptFenceError


NOW = "2026-07-21T12:00:00+00:00"
LEASE_EXPIRES = "2026-07-21T12:01:30+00:00"


def _claim_payload(**overrides):
    payload = {
        "task_kind": "chapter_upload",
        "job_id": "job-1",
        "idempotency_key": "publisher-job:v1:job-1",
        "body_sha256": "a" * 64,
        "platform": "qidian",
        "status": "running",
        "book_name": "Book",
        "chapter_title": "Chapter 1",
        "body": "body",
        "upload_url": None,
        "publish": False,
        "result_payload": {
            "create_if_missing": True,
            "book_meta": {"audience": "male"},
        },
        "abort_requested": False,
        "attempt_id": "attempt-1",
        "attempt_number": 1,
        "attempt_kind": "execute",
        "attempt_status": "running",
        "attempt_phase": "claimed",
        "lease_epoch": 7,
        "lease_expires_at": LEASE_EXPIRES,
        "execution_mode": "execute",
        "server_time": NOW,
        "heartbeat_interval_seconds": 30,
    }
    payload.update(overrides)
    return payload


def _state_payload(**overrides):
    payload = {
        "job_id": "job-1",
        "status": "running",
        "abort_requested": False,
        "attempt_id": "attempt-1",
        "attempt_status": "running",
        "attempt_phase": "claimed",
        "lease_epoch": 7,
        "lease_expires_at": LEASE_EXPIRES,
        "execution_mode": "execute",
        "available_at": "",
        "reconcile_after": "",
        "pause_reason": "",
        "server_time": NOW,
    }
    payload.update(overrides)
    return payload


def _receipt_fields() -> dict[str, object]:
    return {
        "content_sha256": "a" * 64,
        "remote_book_id": "book-remote-1",
        "remote_chapter_id": "chapter-remote-1",
        "remote_url": "https://write.qq.com/chapter/1",
        "official_state": "drafted",
        "observed_at": NOW,
        "evidence": {"heading": "Chapter 1", "content_sha256": "a" * 64},
    }


class _Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failure: Exception | None = None

    def verify_extension_api_key(self, value: str | None) -> None:
        if value != "secret":
            from forwin.publisher_runtime.auth import PublisherExtensionAuthError

            raise PublisherExtensionAuthError("bad key")

    def _respond(self, name: str, kwargs: dict[str, object], payload):
        self.calls.append((name, kwargs))
        if self.failure is not None:
            raise self.failure
        return payload

    def claim_next_upload_job(self, **kwargs):
        return self._respond("claim", kwargs, _claim_payload())

    def heartbeat_upload_attempt(self, **kwargs):
        return self._respond("heartbeat", kwargs, _state_payload())

    def transition_upload_attempt(self, **kwargs):
        return self._respond(
            "phase",
            kwargs,
            _state_payload(attempt_phase=str(kwargs["phase"])),
        )

    def update_upload_job_result(self, **kwargs):
        return self._respond(
            "result",
            kwargs,
            _state_payload(
                status="succeeded",
                attempt_status="succeeded",
                attempt_phase="receipt_observed",
                lease_expires_at="",
            ),
        )

    def record_upload_receipt(self, **kwargs):
        receipt = dict(kwargs["receipt"])
        return self._respond(
            "receipt",
            kwargs,
            _state_payload(
                attempt_phase="receipt_observed",
                receipt_disposition="created",
                protocol_receipt={
                    "receipt_id": "receipt-1",
                    "receipt_key": "b" * 64,
                    **receipt,
                },
            ),
        )

    def reconcile_upload_job(self, **kwargs):
        return self._respond(
            "reconcile",
            kwargs,
            _state_payload(
                status="reconciling",
                attempt_kind="reconcile",
                execution_mode="reconcile",
                attempt_status="indeterminate",
                attempt_phase="observation_started",
                lease_expires_at="",
            ),
        )


def _handlers(manager: _Manager):
    return build_handlers(
        service=PublisherApplicationService(
            get_publisher_manager=lambda: manager,
            extension_root=Path("browser_extension/forwin-publisher"),
        )
    )


def _client(manager: _Manager) -> TestClient:
    runtime = HttpRuntime()
    runtime.publisher_manager = manager
    return TestClient(create_app(runtime))


def test_protocol_requests_forbid_legacy_and_server_owned_fields() -> None:
    with pytest.raises(ValidationError):
        UploadAttemptHeartbeatRequest(
            client_id="extension-1",
            lease_epoch=7,
            lease_seconds=300,
        )
    with pytest.raises(ValidationError):
        UploadAttemptResultRequest(
            client_id="extension-1",
            lease_epoch=7,
            outcome="succeeded",
            phase="receipt_observed",
        )
    with pytest.raises(ValidationError):
        UploadAttemptReceiptRequest(
            client_id="extension-1",
            lease_epoch=7,
            source="journal_sync",
            **_receipt_fields(),
        )
    invalid_evidence = _receipt_fields()
    invalid_evidence["evidence"] = {
        "heading": "Chapter 1",
        "content_sha256": "b" * 64,
    }
    with pytest.raises(ValidationError):
        UploadAttemptReceiptRequest(
            client_id="extension-1",
            lease_epoch=7,
            **invalid_evidence,
        )
    with pytest.raises(ValidationError):
        ExtensionClaimUploadJobRequest(
            client_id="extension-1",
            connected_platforms=["qidian"],
            unexpected=True,
        )
    with pytest.raises(ValidationError):
        UploadAttemptResultRequest(
            client_id="extension-1",
            lease_epoch=7,
            outcome="succeeded",
            details={"result_payload": {"phase": "legacy"}},
        )
    with pytest.raises(ValidationError):
        UploadAttemptReconcileRequest(
            client_id="extension-1",
            lease_epoch=7,
            outcome="indeterminate",
            observed_at=NOW,
            evidence={"arbitrary": "unbounded"},
        )


def test_claim_returns_nested_strict_execution_contract() -> None:
    manager = _Manager()
    response = _handlers(manager)["claim_publisher_upload_job"](
        ExtensionClaimUploadJobRequest(
            client_id="extension-1",
            connected_platforms=["qidian", "qidian"],
        ),
        x_forwin_extension_key="secret",
    )

    assert response.found is True
    assert response.claim is not None
    assert response.claim.execution_mode == "execute"
    assert response.claim.attempt.attempt_id == "attempt-1"
    assert response.claim.attempt.lease_epoch == 7
    assert response.claim.job.task_kind == "chapter_upload"
    assert response.claim.job.content_sha256 == "a" * 64
    assert response.claim.job.input.body == "body"
    assert "body" not in response.model_dump(exclude={"claim"})
    assert manager.calls == [
        (
            "claim",
            {
                "client_id": "extension-1",
                "connected_platforms": ["qidian"],
            },
        )
    ]


def test_attempt_heartbeat_and_phase_forward_path_fence_without_lease_override() -> (
    None
):
    manager = _Manager()
    handlers = _handlers(manager)

    heartbeat = handlers["heartbeat_publisher_upload_attempt"](
        "job-1",
        "attempt-1",
        UploadAttemptHeartbeatRequest(client_id="extension-1", lease_epoch=7),
        x_forwin_extension_key="secret",
    )
    phase = handlers["transition_publisher_upload_attempt"](
        "job-1",
        "attempt-1",
        UploadAttemptPhaseRequest(
            client_id="extension-1",
            lease_epoch=7,
            phase="mutation_started",
            current_url="https://write.qq.com/chapter/create",
        ),
        x_forwin_extension_key="secret",
    )

    assert heartbeat.next_action == "execute"
    assert phase.phase == "mutation_started"
    assert manager.calls == [
        (
            "heartbeat",
            {
                "job_id": "job-1",
                "attempt_id": "attempt-1",
                "worker_id": "extension-1",
                "lease_epoch": 7,
            },
        ),
        (
            "phase",
            {
                "job_id": "job-1",
                "attempt_id": "attempt-1",
                "worker_id": "extension-1",
                "lease_epoch": 7,
                "phase": "mutation_started",
                "current_url": "https://write.qq.com/chapter/create",
            },
        ),
    ]


def test_receipt_precedes_terminal_result_and_server_owns_provenance() -> None:
    manager = _Manager()
    handlers = _handlers(manager)
    receipt = UploadAttemptReceiptRequest(
        client_id="extension-1",
        lease_epoch=7,
        **_receipt_fields(),
    )

    receipt_response = handlers["record_publisher_upload_receipt"](
        "job-1",
        "attempt-1",
        receipt,
        x_forwin_extension_key="secret",
    )
    result_response = handlers["finish_publisher_upload_attempt"](
        "job-1",
        "attempt-1",
        UploadAttemptResultRequest(
            client_id="extension-1",
            lease_epoch=7,
            outcome="succeeded",
            message="saved",
            details={"cover_state": "uploaded"},
        ),
        x_forwin_extension_key="secret",
    )

    assert receipt_response.phase == "receipt_observed"
    assert result_response.job_status == "succeeded"
    assert "body" not in result_response.model_dump()
    assert manager.calls[0] == (
        "receipt",
        {
            "job_id": "job-1",
            "attempt_id": "attempt-1",
            "client_id": "extension-1",
            "lease_epoch": 7,
            "receipt": _receipt_fields(),
        },
    )
    assert manager.calls[1] == (
        "result",
        {
            "job_id": "job-1",
            "attempt_id": "attempt-1",
            "client_id": "extension-1",
            "lease_epoch": 7,
            "outcome": "succeeded",
            "message": "saved",
            "current_url": "",
            "error_code": "",
            "error_message": "",
            "details": {"cover_state": "uploaded"},
        },
    )


def test_reconcile_forwards_read_only_evidence_and_conditional_receipt() -> None:
    manager = _Manager()
    handlers = _handlers(manager)
    receipt = _receipt_fields()

    response = handlers["reconcile_publisher_upload_attempt"](
        "job-1",
        "attempt-2",
        UploadAttemptReconcileRequest(
            client_id="extension-1",
            lease_epoch=8,
            outcome="matched",
            observed_at=NOW,
            evidence={"match": "remote-id-and-content-hash"},
            receipt=receipt,
        ),
        x_forwin_extension_key="secret",
    )

    assert response.job_status == "reconciling"
    assert response.execution_mode == "reconcile"
    assert response.attempt_status == "failed"
    assert manager.calls == [
        (
            "reconcile",
            {
                "job_id": "job-1",
                "attempt_id": "attempt-2",
                "client_id": "extension-1",
                "lease_epoch": 8,
                "outcome": "matched",
                "receipt": receipt,
                "evidence": {"match": "remote-id-and-content-hash"},
                "observed_at": NOW,
                "current_url": "",
                "error_code": "",
                "error_message": "",
            },
        )
    ]


def test_terminating_internal_state_is_exposed_as_running_with_stop_action() -> None:
    class _TerminatingManager(_Manager):
        def heartbeat_upload_attempt(self, **kwargs):
            return self._respond(
                "heartbeat",
                kwargs,
                _state_payload(status="terminating", abort_requested=True),
            )

    response = _handlers(_TerminatingManager())["heartbeat_publisher_upload_attempt"](
        "job-1",
        "attempt-1",
        UploadAttemptHeartbeatRequest(client_id="extension-1", lease_epoch=7),
        x_forwin_extension_key="secret",
    )

    assert response.job_status == "running"
    assert response.abort_requested is True
    assert response.next_action == "stop"


def test_abort_request_does_not_block_read_only_reconciliation() -> None:
    class _AbortedReconcileManager(_Manager):
        def heartbeat_upload_attempt(self, **kwargs):
            return self._respond(
                "heartbeat",
                kwargs,
                _state_payload(
                    abort_requested=True,
                    execution_mode="reconcile",
                    attempt_phase="observation_started",
                ),
            )

    response = _handlers(_AbortedReconcileManager())[
        "heartbeat_publisher_upload_attempt"
    ](
        "job-1",
        "attempt-1",
        UploadAttemptHeartbeatRequest(client_id="extension-1", lease_epoch=7),
        x_forwin_extension_key="secret",
    )

    assert response.abort_requested is True
    assert response.execution_mode == "reconcile"
    assert response.next_action == "reconcile"


def test_stale_fence_is_structured_conflict_and_auth_is_required() -> None:
    manager = _Manager()
    manager.failure = PublisherAttemptFenceError(
        "publisher attempt fence is no longer current",
        job_status="succeeded",
        current_attempt_id="",
    )
    handlers = _handlers(manager)
    req = UploadAttemptHeartbeatRequest(client_id="extension-1", lease_epoch=7)

    with pytest.raises(HTTPException) as stale:
        handlers["heartbeat_publisher_upload_attempt"](
            "job-1",
            "attempt-1",
            req,
            x_forwin_extension_key="secret",
        )
    with pytest.raises(HTTPException) as unauthorized:
        handlers["heartbeat_publisher_upload_attempt"](
            "job-1",
            "attempt-1",
            req,
            x_forwin_extension_key=None,
        )

    assert stale.value.status_code == 409
    assert stale.value.detail == {
        "code": "stale_attempt",
        "message": "publisher attempt fence is no longer current",
        "job_status": "succeeded",
        "current_attempt_id": "",
    }
    assert unauthorized.value.status_code == 401
    assert unauthorized.value.detail["code"] == "invalid_extension_key"


def test_http_route_table_exposes_only_six_extension_attempt_routes() -> None:
    source = Path("forwin/http/routes.py").read_text(encoding="utf-8")
    expected = (
        "/api/publishers/extension/upload-jobs/claim",
        "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/heartbeat",
        "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/phase",
        "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/result",
        "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/receipt",
        "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/reconcile",
    )

    for path in expected:
        assert path in source
    assert '"/api/publishers/upload-jobs/{job_id}/result"' not in source


def test_real_http_protocol_preserves_nested_claim_and_structured_validation() -> None:
    client = _client(_Manager())
    headers = {"X-Forwin-Extension-Key": "secret"}

    claim = client.post(
        "/api/publishers/extension/upload-jobs/claim",
        headers=headers,
        json={
            "client_id": "extension-1",
            "connected_platforms": ["qidian"],
        },
    )
    invalid = client.post(
        "/api/publishers/extension/upload-jobs/job-1/attempts/attempt-1/heartbeat",
        headers=headers,
        json={
            "client_id": "extension-1",
            "lease_epoch": 7,
            "lease_seconds": 300,
        },
    )
    removed = client.post(
        "/api/publishers/upload-jobs/job-1/result",
        headers=headers,
        json={},
    )

    assert claim.status_code == 200
    assert claim.json()["claim"]["attempt"]["attempt_id"] == "attempt-1"
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "validation_error"
    assert removed.status_code == 404
