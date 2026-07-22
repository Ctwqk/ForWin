from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherUploadJob,
    PublisherUploadReceipt,
)
from forwin.publisher_runtime.receipts import PublisherReceiptService
from tests.test_canon_publisher_jobs import _fixture, _materialize


NOW = datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)


def test_receipt_key_uses_stable_remote_ids_instead_of_mutable_urls() -> None:
    common = {
        "job_identity": "publisher-job:v1:job-1",
        "platform_id": "qidian",
        "remote_book_id": "book-1",
        "remote_chapter_id": "chapter-1",
        "content_sha256": "a" * 64,
    }

    assert PublisherReceiptService.receipt_key(
        **common,
        remote_url="https://write.qq.com/chapter/1",
    ) == PublisherReceiptService.receipt_key(
        **common,
        remote_url="https://write.qq.com/chapter/1?token=rotated",
    )


def test_cover_receipt_can_monotonically_add_a_canonical_remote_url() -> None:
    fixture = _fixture("publisher-cover-receipt-url-enrichment")
    content_sha256 = "c" * 64
    try:
        with fixture.runtime.session_factory() as session:
            session.add(
                PublisherUploadJob(
                    id="cover-job",
                    project_id=fixture.project_id,
                    idempotency_key="publisher-job:v1:cover-job",
                    platform_id="qidian",
                    task_kind="cover_upload",
                    status="pending",
                    book_name="封面作品",
                    body_text="",
                    body_sha256=content_sha256,
                    publish=False,
                    result_payload_json=json.dumps(
                        {
                            "work_binding_id": "work-cover",
                            "remote_book_id": "book-cover",
                            "cover_asset_id": "asset-cover",
                            "file_path": "/tmp/cover.png",
                        }
                    ),
                )
            )
            session.commit()
        claim = fixture.runtime.attempts.claim(
            client_id="extension-cover-receipt",
            connected_platforms=["qidian"],
            now=NOW,
        )
        assert claim is not None
        fixture.runtime.attempts.transition(
            job_id="cover-job",
            attempt_id=claim["attempt_id"],
            worker_id="extension-cover-receipt",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        receipt = {
            "remote_book_id": "book-cover",
            "remote_chapter_id": "",
            "remote_url": "",
            "official_state": "cover_uploaded",
            "content_sha256": content_sha256,
            "evidence": {
                "content_sha256": content_sha256,
                "confirmation_text": "accepted",
            },
        }
        wrong_target = {**receipt, "remote_book_id": "book-other"}
        with pytest.raises(ValueError, match="does not match the claimed job"):
            fixture.runtime.upload_jobs.record_upload_receipt(
                job_id="cover-job",
                attempt_id=claim["attempt_id"],
                client_id="extension-cover-receipt",
                lease_epoch=claim["lease_epoch"],
                receipt=wrong_target,
                now=NOW + timedelta(seconds=2),
            )
        fixture.runtime.upload_jobs.record_upload_receipt(
            job_id="cover-job",
            attempt_id=claim["attempt_id"],
            client_id="extension-cover-receipt",
            lease_epoch=claim["lease_epoch"],
            receipt=receipt,
            now=NOW + timedelta(seconds=2),
        )
        enriched_receipt = {
            **receipt,
            "remote_url": "https://write.qq.com/portal/book/cover",
        }
        enriched = fixture.runtime.upload_jobs.record_upload_receipt(
            job_id="cover-job",
            attempt_id=claim["attempt_id"],
            client_id="extension-cover-receipt",
            lease_epoch=claim["lease_epoch"],
            receipt=enriched_receipt,
            now=NOW + timedelta(seconds=3),
        )

        assert enriched["receipt_disposition"] == "duplicate"
        assert enriched["protocol_receipt"]["remote_url"] == (
            "https://write.qq.com/portal/book/cover"
        )
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadReceipt.id))) == 1
    finally:
        fixture.engine.dispose()


def _uncertain_job(fixture) -> tuple[dict, dict]:
    created = _materialize(fixture)
    released = fixture.runtime.canon_jobs.release(
        project_id=fixture.project_id,
        job_ids=[created[0]["job_id"]],
        publish=True,
        actor_type="scheduler",
    )[0]
    claimed = fixture.runtime.attempts.claim(
        client_id="extension-execute",
        connected_platforms=["qidian"],
        lease_seconds=10,
        now=NOW,
    )
    assert claimed is not None
    fixture.runtime.attempts.transition(
        job_id=released["job_id"],
        attempt_id=claimed["attempt_id"],
        worker_id="extension-execute",
        lease_epoch=claimed["lease_epoch"],
        phase="mutation_started",
        lease_seconds=10,
        now=NOW + timedelta(seconds=1),
    )
    fixture.runtime.attempts.expire(now=NOW + timedelta(seconds=12))
    return released, claimed


def _receipt(job: dict, suffix: str = "1") -> dict:
    return {
        "remote_book_id": "book-remote-1",
        "remote_chapter_id": f"chapter-remote-{suffix}",
        "remote_url": f"https://write.qq.com/chapter/{suffix}",
        "official_state": "drafted",
        "content_sha256": job["body_sha256"],
        "evidence": {
            "selector": "chapter-row",
            "matched": True,
            "content_sha256": job["body_sha256"],
        },
    }


def _claim_reconciliation(fixture, *, client_id: str) -> dict:
    claimed_at = NOW + timedelta(seconds=13)
    claim = fixture.runtime.attempts.claim(
        client_id=client_id,
        connected_platforms=["qidian"],
        lease_seconds=30,
        now=claimed_at,
    )
    assert claim is not None
    fixture.runtime.attempts.transition(
        job_id=claim["job_id"],
        attempt_id=claim["attempt_id"],
        worker_id=client_id,
        lease_epoch=claim["lease_epoch"],
        phase="observation_started",
        now=claimed_at,
    )
    return claim


def test_reconciliation_match_succeeds_with_one_idempotent_receipt() -> None:
    fixture = _fixture("publisher-reconcile-match")
    try:
        job, _execute = _uncertain_job(fixture)
        reconcile = _claim_reconciliation(
            fixture,
            client_id="extension-reconcile",
        )

        first = fixture.runtime.upload_jobs.reconcile_upload_job(
            job_id=job["job_id"],
            client_id="extension-reconcile",
            attempt_id=reconcile["attempt_id"],
            lease_epoch=reconcile["lease_epoch"],
            outcome="matched",
            receipt=_receipt(job),
            evidence={"scan": "complete"},
            now=NOW + timedelta(seconds=14),
        )
        replay = fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=job["job_id"],
            attempt_id=reconcile["attempt_id"],
            client_id="extension-reconcile",
            lease_epoch=reconcile["lease_epoch"],
            receipt=_receipt(job),
            now=NOW + timedelta(seconds=15),
        )

        assert first["status"] == "succeeded"
        assert replay["status"] == "succeeded"
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadReceipt.id))) == 1
            chapter = session.execute(select(PublisherChapterBinding)).scalar_one()
            assert chapter.remote_chapter_id == "chapter-remote-1"
            assert chapter.remote_url == "https://write.qq.com/chapter/1"
            assert chapter.publish_state == "drafted"
    finally:
        fixture.engine.dispose()


def test_authoritative_absence_alone_permits_new_execute_attempt() -> None:
    fixture = _fixture("publisher-reconcile-absent")
    fixture.runtime.attempts.authoritative_absence_platforms = frozenset({"qidian"})
    try:
        job, _execute = _uncertain_job(fixture)
        reconcile = _claim_reconciliation(
            fixture,
            client_id="extension-reconcile-absent",
        )

        resolved = fixture.runtime.upload_jobs.reconcile_upload_job(
            job_id=job["job_id"],
            client_id="extension-reconcile-absent",
            attempt_id=reconcile["attempt_id"],
            lease_epoch=reconcile["lease_epoch"],
            outcome="absent",
            receipt=None,
            evidence={"pagination_complete": True},
            now=NOW + timedelta(seconds=14),
        )
        assert resolved["status"] == "pending"

        execute_again = fixture.runtime.attempts.claim(
            client_id="extension-execute-again",
            connected_platforms=["qidian"],
            lease_seconds=30,
            now=NOW + timedelta(seconds=15),
        )
        assert execute_again is not None
        assert execute_again["execution_mode"] == "execute"
        assert execute_again["attempt_number"] == 3
    finally:
        fixture.engine.dispose()


def test_nonauthoritative_absence_is_rejected_without_mutation() -> None:
    fixture = _fixture("publisher-reconcile-absent-rejected")
    try:
        job, _execute = _uncertain_job(fixture)
        reconcile = _claim_reconciliation(
            fixture,
            client_id="extension-reconcile-no-absence",
        )

        with pytest.raises(ValueError, match="authoritative absence"):
            fixture.runtime.upload_jobs.reconcile_upload_job(
                job_id=job["job_id"],
                client_id="extension-reconcile-no-absence",
                attempt_id=reconcile["attempt_id"],
                lease_epoch=reconcile["lease_epoch"],
                outcome="absent",
                receipt=None,
                evidence={"partial_scan": True},
                now=NOW + timedelta(seconds=14),
            )

        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["status"]
            == "running"
        )
    finally:
        fixture.engine.dispose()


def test_indeterminate_reconciliation_never_returns_to_pending() -> None:
    fixture = _fixture("publisher-reconcile-indeterminate")
    try:
        job, _execute = _uncertain_job(fixture)
        reconcile = _claim_reconciliation(
            fixture,
            client_id="extension-reconcile-indeterminate",
        )

        result = fixture.runtime.upload_jobs.reconcile_upload_job(
            job_id=job["job_id"],
            client_id="extension-reconcile-indeterminate",
            attempt_id=reconcile["attempt_id"],
            lease_epoch=reconcile["lease_epoch"],
            outcome="indeterminate",
            receipt=None,
            evidence={"reason": "pagination unavailable"},
            now=NOW + timedelta(seconds=14),
        )

        assert result["status"] == "reconciling"
    finally:
        fixture.engine.dispose()


def test_old_failure_cannot_overwrite_reconciled_success() -> None:
    fixture = _fixture("publisher-reconcile-monotonic")
    try:
        job, execute = _uncertain_job(fixture)
        reconcile = _claim_reconciliation(
            fixture,
            client_id="extension-reconcile-success",
        )
        fixture.runtime.upload_jobs.reconcile_upload_job(
            job_id=job["job_id"],
            client_id="extension-reconcile-success",
            attempt_id=reconcile["attempt_id"],
            lease_epoch=reconcile["lease_epoch"],
            outcome="matched",
            receipt=_receipt(job),
            evidence={},
            now=NOW + timedelta(seconds=14),
        )

        with pytest.raises(ValueError, match="fence is no longer current"):
            fixture.runtime.upload_jobs.update_upload_job_result(
                job_id=job["job_id"],
                client_id="extension-execute",
                attempt_id=execute["attempt_id"],
                lease_epoch=execute["lease_epoch"],
                outcome="failed",
                message="late failure",
                current_url="",
                error_code="timeout",
                error_message="timeout",
                details={},
                now=NOW + timedelta(seconds=15),
            )

        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["status"]
            == "succeeded"
        )
    finally:
        fixture.engine.dispose()


def test_late_valid_receipt_moves_job_monotonically_to_success() -> None:
    fixture = _fixture("publisher-late-receipt")
    try:
        job, execute = _uncertain_job(fixture)

        result = fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=job["job_id"],
            attempt_id=execute["attempt_id"],
            client_id="extension-execute",
            lease_epoch=execute["lease_epoch"],
            receipt=_receipt(job, suffix="late"),
            now=NOW + timedelta(seconds=13),
        )

        assert result["status"] == "succeeded"
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadReceipt.id))) == 1
    finally:
        fixture.engine.dispose()


def test_current_receipt_is_durable_before_terminal_success() -> None:
    fixture = _fixture("publisher-current-receipt-first")
    try:
        created = _materialize(fixture)
        job = fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[created[0]["job_id"]],
            publish=False,
            actor_type="scheduler",
        )[0]
        claim = fixture.runtime.attempts.claim(
            client_id="extension-current",
            connected_platforms=["qidian"],
            lease_seconds=30,
            now=NOW,
        )
        assert claim is not None
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id="extension-current",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )

        recorded = fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            client_id="extension-current",
            lease_epoch=claim["lease_epoch"],
            receipt=_receipt(job),
            now=NOW + timedelta(seconds=2),
        )
        duplicate = fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            client_id="extension-current",
            lease_epoch=claim["lease_epoch"],
            receipt=_receipt(job),
            now=NOW + timedelta(seconds=3),
        )

        assert recorded["status"] == "running"
        assert recorded["attempt_phase"] == "receipt_observed"
        assert recorded["receipt_disposition"] == "created"
        assert duplicate["status"] == "running"
        assert duplicate["receipt_disposition"] == "duplicate"

        with pytest.raises(ValueError, match="cannot report a negative result"):
            fixture.runtime.upload_jobs.update_upload_job_result(
                job_id=job["job_id"],
                client_id="extension-current",
                attempt_id=claim["attempt_id"],
                lease_epoch=claim["lease_epoch"],
                outcome="failed",
                message="contradictory failure",
                current_url="",
                error_code="post_receipt_error",
                error_message="receipt already proves success",
                details={},
                now=NOW + timedelta(seconds=4),
            )
        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["status"]
            == "running"
        )

        finished = fixture.runtime.upload_jobs.update_upload_job_result(
            job_id=job["job_id"],
            client_id="extension-current",
            attempt_id=claim["attempt_id"],
            lease_epoch=claim["lease_epoch"],
            outcome="succeeded",
            message="saved",
            current_url="https://write.qq.com/untrusted/result/url",
            error_code="",
            error_message="",
            details={},
            now=NOW + timedelta(seconds=5),
        )

        assert finished["status"] == "succeeded"
        assert finished["attempt_status"] == "succeeded"
        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["current_url"]
            == "https://write.qq.com/chapter/1"
        )
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadReceipt.id))) == 1
    finally:
        fixture.engine.dispose()


def test_mutating_success_without_receipt_is_rejected() -> None:
    fixture = _fixture("publisher-success-needs-receipt")
    try:
        created = _materialize(fixture)
        job = fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[created[0]["job_id"]],
            publish=False,
            actor_type="scheduler",
        )[0]
        claim = fixture.runtime.attempts.claim(
            client_id="extension-no-receipt",
            connected_platforms=["qidian"],
            lease_seconds=30,
            now=NOW,
        )
        assert claim is not None

        with pytest.raises(ValueError, match="requires a durable receipt"):
            fixture.runtime.upload_jobs.update_upload_job_result(
                job_id=job["job_id"],
                client_id="extension-no-receipt",
                attempt_id=claim["attempt_id"],
                lease_epoch=claim["lease_epoch"],
                outcome="succeeded",
                message="saved",
                current_url="",
                error_code="",
                error_message="",
                details={},
                now=NOW + timedelta(seconds=1),
            )

        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["status"]
            == "running"
        )
    finally:
        fixture.engine.dispose()


def test_reconciliation_risk_pause_blocks_new_claims() -> None:
    fixture = _fixture("publisher-reconcile-risk-pause")
    try:
        job, _execute = _uncertain_job(fixture)
        reconcile = _claim_reconciliation(
            fixture,
            client_id="extension-risk",
        )

        paused = fixture.runtime.upload_jobs.reconcile_upload_job(
            job_id=job["job_id"],
            client_id="extension-risk",
            attempt_id=reconcile["attempt_id"],
            lease_epoch=reconcile["lease_epoch"],
            outcome="risk_pause",
            receipt=None,
            evidence={"reason": "captcha visible", "risk_reason": "captcha"},
            now=NOW + timedelta(seconds=14),
        )

        assert paused["status"] == "paused"
        assert paused["pause_reason"] == "captcha"
        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-risk-next",
                connected_platforms=["qidian"],
                now=NOW + timedelta(seconds=15),
            )
            is None
        )
    finally:
        fixture.engine.dispose()


def test_reconciliation_requires_observation_phase_acknowledgement() -> None:
    fixture = _fixture("publisher-reconcile-phase-required")
    try:
        job, _execute = _uncertain_job(fixture)
        reconcile = fixture.runtime.attempts.claim(
            client_id="extension-reconcile-without-phase",
            connected_platforms=["qidian"],
            now=NOW + timedelta(seconds=13),
        )
        assert reconcile is not None

        with pytest.raises(ValueError, match="must use the reconcile endpoint"):
            fixture.runtime.upload_jobs.update_upload_job_result(
                job_id=job["job_id"],
                client_id="extension-reconcile-without-phase",
                attempt_id=reconcile["attempt_id"],
                lease_epoch=reconcile["lease_epoch"],
                outcome="failed",
                message="read-only scan failed",
                current_url="",
                error_code="scan_failed",
                error_message="scan failed",
                details={},
                now=NOW + timedelta(seconds=14),
            )
        with pytest.raises(ValueError, match="requires observation_started"):
            fixture.runtime.upload_jobs.reconcile_upload_job(
                job_id=job["job_id"],
                client_id="extension-reconcile-without-phase",
                attempt_id=reconcile["attempt_id"],
                lease_epoch=reconcile["lease_epoch"],
                outcome="indeterminate",
                receipt=None,
                evidence={"reason": "not started"},
                now=NOW + timedelta(seconds=14),
            )

        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["status"]
            == "running"
        )
    finally:
        fixture.engine.dispose()


def test_receipt_rejects_unstable_or_cross_platform_remote_urls() -> None:
    fixture = _fixture("publisher-receipt-url-proof")
    try:
        created = _materialize(fixture)
        job = fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[created[0]["job_id"]],
            publish=False,
            actor_type="scheduler",
        )[0]
        claim = fixture.runtime.attempts.claim(
            client_id="extension-url-proof",
            connected_platforms=["qidian"],
            now=NOW,
        )
        assert claim is not None
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id="extension-url-proof",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )

        wrong_hash = _receipt(job)
        wrong_hash["content_sha256"] = "b" * 64
        with pytest.raises(ValueError, match="content hash mismatch"):
            fixture.runtime.upload_jobs.record_upload_receipt(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                client_id="extension-url-proof",
                lease_epoch=claim["lease_epoch"],
                receipt=wrong_hash,
                now=NOW + timedelta(seconds=2),
            )

        wrong_evidence_hash = _receipt(job)
        wrong_evidence_hash["evidence"]["content_sha256"] = "b" * 64
        with pytest.raises(ValueError, match="evidence content hash mismatch"):
            fixture.runtime.upload_jobs.record_upload_receipt(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                client_id="extension-url-proof",
                lease_epoch=claim["lease_epoch"],
                receipt=wrong_evidence_hash,
                now=NOW + timedelta(seconds=2),
            )

        unstable = _receipt(job)
        unstable["remote_chapter_id"] = ""
        unstable["remote_url"] = "https://write.qq.com/portal/dashboard"
        with pytest.raises(ValueError, match="stable remote chapter identity"):
            fixture.runtime.upload_jobs.record_upload_receipt(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                client_id="extension-url-proof",
                lease_epoch=claim["lease_epoch"],
                receipt=unstable,
                now=NOW + timedelta(seconds=3),
            )

        cross_platform = _receipt(job)
        cross_platform["remote_url"] = "https://example.test/chapter/1"
        with pytest.raises(ValueError, match="does not belong to qidian"):
            fixture.runtime.upload_jobs.record_upload_receipt(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                client_id="extension-url-proof",
                lease_epoch=claim["lease_epoch"],
                receipt=cross_platform,
                now=NOW + timedelta(seconds=4),
            )

        conflicting_book = _receipt(job)
        conflicting_book["remote_book_id"] = "111"
        conflicting_book["remote_chapter_id"] = "333"
        conflicting_book["remote_url"] = (
            "https://write.qq.com/portal/booknovels/chaptertmp/CBID/222?ccid=333"
        )
        with pytest.raises(ValueError, match="book ID conflicts"):
            fixture.runtime.upload_jobs.record_upload_receipt(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                client_id="extension-url-proof",
                lease_epoch=claim["lease_epoch"],
                receipt=conflicting_book,
                now=NOW + timedelta(seconds=5),
            )

        conflicting_chapter = dict(conflicting_book)
        conflicting_chapter["remote_book_id"] = "222"
        conflicting_chapter["remote_chapter_id"] = "444"
        with pytest.raises(ValueError, match="chapter ID conflicts"):
            fixture.runtime.upload_jobs.record_upload_receipt(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                client_id="extension-url-proof",
                lease_epoch=claim["lease_epoch"],
                receipt=conflicting_chapter,
                now=NOW + timedelta(seconds=6),
            )

        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadReceipt.id))) == 0
    finally:
        fixture.engine.dispose()


def test_one_job_cannot_accumulate_conflicting_remote_receipts() -> None:
    fixture = _fixture("publisher-receipt-one-job-one-identity")
    try:
        created = _materialize(fixture)
        job = fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[created[0]["job_id"]],
            publish=False,
            actor_type="scheduler",
        )[0]
        claim = fixture.runtime.attempts.claim(
            client_id="extension-receipt-conflict",
            connected_platforms=["qidian"],
            now=NOW,
        )
        assert claim is not None
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id="extension-receipt-conflict",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            client_id="extension-receipt-conflict",
            lease_epoch=claim["lease_epoch"],
            receipt=_receipt(job, suffix="first"),
            now=NOW + timedelta(seconds=2),
        )

        with pytest.raises(ValueError, match="immutable publisher receipt mismatch"):
            fixture.runtime.upload_jobs.record_upload_receipt(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                client_id="extension-receipt-conflict",
                lease_epoch=claim["lease_epoch"],
                receipt=_receipt(job, suffix="second"),
                now=NOW + timedelta(seconds=3),
            )

        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadReceipt.id))) == 1
    finally:
        fixture.engine.dispose()


def test_receipt_replay_canonicalizes_url_and_enriches_official_state() -> None:
    fixture = _fixture("publisher-receipt-canonical-url")
    try:
        created = _materialize(fixture)
        job = fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[created[0]["job_id"]],
            publish=False,
            actor_type="scheduler",
        )[0]
        claim = fixture.runtime.attempts.claim(
            client_id="extension-canonical",
            connected_platforms=["qidian"],
            now=NOW,
        )
        assert claim is not None
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id="extension-canonical",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        first_receipt = _receipt(job)
        first_receipt["remote_chapter_id"] = ""
        first_receipt["remote_url"] = (
            "https://write.qq.com/chaptertmp/123?ccid=987654321&token=one#draft"
        )
        fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            client_id="extension-canonical",
            lease_epoch=claim["lease_epoch"],
            receipt=first_receipt,
            now=NOW + timedelta(seconds=2),
        )
        enriched_receipt = dict(first_receipt)
        enriched_receipt["remote_url"] = (
            "https://write.qq.com/chaptertmp/123?token=two&ccid=987654321"
        )
        enriched_receipt["official_state"] = "published"
        enriched_receipt["evidence"] = {
            **first_receipt["evidence"],
            "matched": False,
            "confirmation_text": "published",
        }
        enriched = fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            client_id="extension-canonical",
            lease_epoch=claim["lease_epoch"],
            receipt=enriched_receipt,
            now=NOW + timedelta(seconds=3),
        )

        assert enriched["receipt_disposition"] == "duplicate"
        assert enriched["protocol_receipt"]["remote_chapter_id"] == "987654321"
        assert enriched["protocol_receipt"]["remote_url"] == (
            "https://write.qq.com/chaptertmp/123?ccid=987654321"
        )
        assert enriched["protocol_receipt"]["official_state"] == "published"
        assert enriched["protocol_receipt"]["evidence"]["matched"] is True
        assert (
            enriched["protocol_receipt"]["evidence"]["confirmation_text"] == "published"
        )
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadReceipt.id))) == 1
    finally:
        fixture.engine.dispose()
