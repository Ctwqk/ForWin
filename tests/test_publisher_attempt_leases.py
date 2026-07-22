from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from forwin.models.publisher import PublisherUploadAttempt, PublisherUploadJob
from tests.test_canon_publisher_jobs import _fixture, _materialize


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _released_job(fixture) -> dict:
    created = _materialize(fixture)
    released = fixture.runtime.canon_jobs.release(
        project_id=fixture.project_id,
        job_ids=[created[0]["job_id"]],
        publish=True,
        actor_type="scheduler",
    )
    return released[0]


def test_execute_claim_creates_fenced_attempt() -> None:
    fixture = _fixture("publisher-attempt-claim")
    try:
        job = _released_job(fixture)

        claimed = fixture.runtime.attempts.claim(
            client_id="extension-a",
            connected_platforms=["qidian"],
            lease_seconds=30,
            now=NOW,
        )

        assert claimed is not None
        assert claimed["job_id"] == job["job_id"]
        assert claimed["execution_mode"] == "execute"
        assert claimed["attempt_id"]
        assert claimed["attempt_number"] == 1
        assert claimed["lease_epoch"] == 1
        assert claimed["attempt_phase"] == "claimed"
        with fixture.runtime.session_factory() as session:
            stored_job = session.get(PublisherUploadJob, job["job_id"])
            attempt = session.get(PublisherUploadAttempt, claimed["attempt_id"])
            assert stored_job is not None
            assert attempt is not None
            assert stored_job.current_attempt_id == attempt.id
            assert attempt.worker_id == "extension-a"
            assert attempt.status == "running"
    finally:
        fixture.engine.dispose()


def test_claim_replay_is_allowed_only_before_remote_work_starts() -> None:
    fixture = _fixture("publisher-attempt-claim-replay-boundary")
    try:
        job = _released_job(fixture)
        first = fixture.runtime.attempts.claim(
            client_id="extension-replay",
            connected_platforms=["qidian"],
            lease_seconds=30,
            now=NOW,
        )
        replay = fixture.runtime.attempts.claim(
            client_id="extension-replay",
            connected_platforms=["qidian"],
            lease_seconds=30,
            now=NOW + timedelta(seconds=1),
        )
        assert first is not None
        assert replay is not None
        assert replay["attempt_id"] == first["attempt_id"]
        assert replay["attempt_phase"] == "claimed"

        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=first["attempt_id"],
            worker_id="extension-replay",
            lease_epoch=first["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=2),
        )

        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-replay",
                connected_platforms=["qidian"],
                now=NOW + timedelta(seconds=3),
            )
            is None
        )
    finally:
        fixture.engine.dispose()


def test_worker_cannot_hide_an_active_platform_to_claim_another_job() -> None:
    fixture = _fixture("publisher-attempt-one-active-per-worker")
    try:
        created = _materialize(
            fixture,
            bindings=[
                {
                    "platform": "qidian",
                    "book_name": "起点作品",
                    "create_if_missing": False,
                },
                {
                    "platform": "fanqie",
                    "book_name": "番茄作品",
                    "create_if_missing": False,
                },
            ],
        )
        fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[item["job_id"] for item in created],
            publish=False,
            actor_type="scheduler",
        )
        first = fixture.runtime.attempts.claim(
            client_id="extension-cross-platform",
            connected_platforms=["qidian"],
            now=NOW,
        )

        assert first is not None
        assert first["platform"] == "qidian"
        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-cross-platform",
                connected_platforms=["fanqie"],
                now=NOW + timedelta(seconds=1),
            )
            is None
        )
    finally:
        fixture.engine.dispose()


def test_attempt_heartbeat_extends_only_the_current_fence() -> None:
    fixture = _fixture("publisher-attempt-heartbeat")
    try:
        _released_job(fixture)
        claimed = fixture.runtime.attempts.claim(
            client_id="extension-heartbeat",
            connected_platforms=["qidian"],
            lease_seconds=20,
            now=NOW,
        )
        assert claimed is not None

        heartbeat = fixture.runtime.attempts.heartbeat(
            job_id=claimed["job_id"],
            attempt_id=claimed["attempt_id"],
            worker_id="extension-heartbeat",
            lease_epoch=claimed["lease_epoch"],
            lease_seconds=45,
            now=NOW + timedelta(seconds=5),
        )
        assert (
            heartbeat["lease_expires_at"] == (NOW + timedelta(seconds=50)).isoformat()
        )

        with pytest.raises(ValueError, match="fence is no longer current"):
            fixture.runtime.attempts.heartbeat(
                job_id=claimed["job_id"],
                attempt_id=claimed["attempt_id"],
                worker_id="extension-heartbeat",
                lease_epoch=claimed["lease_epoch"] + 1,
                lease_seconds=45,
                now=NOW + timedelta(seconds=6),
            )
    finally:
        fixture.engine.dispose()


def test_attempt_fence_rejects_wrong_worker_expiry_and_backward_phase() -> None:
    fixture = _fixture("publisher-attempt-strict-fence")
    try:
        job = _released_job(fixture)
        claimed = fixture.runtime.attempts.claim(
            client_id="extension-owner",
            connected_platforms=["qidian"],
            lease_seconds=10,
            now=NOW,
        )
        assert claimed is not None

        with pytest.raises(ValueError, match="fence is no longer current"):
            fixture.runtime.attempts.heartbeat(
                job_id=job["job_id"],
                attempt_id=claimed["attempt_id"],
                worker_id="extension-other",
                lease_epoch=claimed["lease_epoch"],
                now=NOW + timedelta(seconds=1),
            )

        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=claimed["attempt_id"],
            worker_id="extension-owner",
            lease_epoch=claimed["lease_epoch"],
            phase="mutation_started",
            lease_seconds=10,
            now=NOW + timedelta(seconds=1),
        )
        with pytest.raises(ValueError, match="cannot move backwards"):
            fixture.runtime.attempts.transition(
                job_id=job["job_id"],
                attempt_id=claimed["attempt_id"],
                worker_id="extension-owner",
                lease_epoch=claimed["lease_epoch"],
                phase="claimed",
                now=NOW + timedelta(seconds=2),
            )
        with pytest.raises(ValueError, match="lease has expired"):
            fixture.runtime.attempts.heartbeat(
                job_id=job["job_id"],
                attempt_id=claimed["attempt_id"],
                worker_id="extension-owner",
                lease_epoch=claimed["lease_epoch"],
                now=NOW + timedelta(seconds=11),
            )
    finally:
        fixture.engine.dispose()


def test_expired_pre_mutation_attempt_returns_to_pending() -> None:
    fixture = _fixture("publisher-attempt-expire-before-mutation")
    try:
        job = _released_job(fixture)
        claimed = fixture.runtime.attempts.claim(
            client_id="extension-expire-before",
            connected_platforms=["qidian"],
            lease_seconds=10,
            now=NOW,
        )
        assert claimed is not None

        expired = fixture.runtime.attempts.expire(now=NOW + timedelta(seconds=11))

        assert expired == [job["job_id"]]
        stored = fixture.runtime.upload_jobs.get_upload_job(job["job_id"])
        assert stored["status"] == "pending"
        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-expire-before",
                connected_platforms=["qidian"],
                lease_seconds=10,
                now=NOW + timedelta(seconds=12),
            )
            is None
        )
        with fixture.runtime.session_factory() as session:
            stored_job = session.get(PublisherUploadJob, job["job_id"])
            attempt = session.get(PublisherUploadAttempt, claimed["attempt_id"])
            assert stored_job is not None
            assert attempt is not None
            assert stored_job.available_at is not None
            assert stored_job.available_at.replace(tzinfo=timezone.utc) == (
                NOW + timedelta(seconds=13)
            )
            assert attempt.status == "expired"
    finally:
        fixture.engine.dispose()


def test_expired_post_mutation_attempt_requires_reconciliation() -> None:
    fixture = _fixture("publisher-attempt-expire-after-mutation")
    try:
        job = _released_job(fixture)
        claimed = fixture.runtime.attempts.claim(
            client_id="extension-expire-after",
            connected_platforms=["qidian"],
            lease_seconds=10,
            now=NOW,
        )
        assert claimed is not None
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=claimed["attempt_id"],
            worker_id="extension-expire-after",
            lease_epoch=claimed["lease_epoch"],
            phase="mutation_started",
            lease_seconds=10,
            now=NOW + timedelta(seconds=1),
        )

        fixture.runtime.attempts.expire(now=NOW + timedelta(seconds=12))

        stored = fixture.runtime.upload_jobs.get_upload_job(job["job_id"])
        assert stored["status"] == "reconciling"
        reconciliation = fixture.runtime.attempts.claim(
            client_id="extension-reconcile",
            connected_platforms=["qidian"],
            lease_seconds=30,
            now=NOW + timedelta(seconds=13),
        )
        assert reconciliation is not None
        assert reconciliation["execution_mode"] == "reconcile"
        assert reconciliation["attempt_number"] == 2
        assert reconciliation["lease_epoch"] == 2
    finally:
        fixture.engine.dispose()


def test_abort_before_mutation_expires_to_cancelled() -> None:
    fixture = _fixture("publisher-attempt-abort-before-mutation")
    try:
        job = _released_job(fixture)
        claimed = fixture.runtime.attempts.claim(
            client_id="extension-abort-before",
            connected_platforms=["qidian"],
            lease_seconds=10,
            now=NOW,
        )
        assert claimed is not None
        fixture.runtime.upload_jobs.terminate_upload_job(job["job_id"])

        with pytest.raises(ValueError, match="after abort was requested"):
            fixture.runtime.attempts.transition(
                job_id=job["job_id"],
                attempt_id=claimed["attempt_id"],
                worker_id="extension-abort-before",
                lease_epoch=claimed["lease_epoch"],
                phase="mutation_started",
                now=NOW + timedelta(seconds=1),
            )

        fixture.runtime.attempts.expire(now=NOW + timedelta(seconds=10))

        stored = fixture.runtime.upload_jobs.get_upload_job(job["job_id"])
        assert stored["status"] == "cancelled"
        assert stored["abort_requested"] is True
    finally:
        fixture.engine.dispose()


def test_abort_after_mutation_still_allows_read_only_reconciliation() -> None:
    fixture = _fixture("publisher-attempt-abort-after-mutation")
    fixture.runtime.attempts.authoritative_absence_platforms = frozenset({"qidian"})
    try:
        job = _released_job(fixture)
        execute = fixture.runtime.attempts.claim(
            client_id="extension-abort-after",
            connected_platforms=["qidian"],
            lease_seconds=10,
            now=NOW,
        )
        assert execute is not None
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=execute["attempt_id"],
            worker_id="extension-abort-after",
            lease_epoch=execute["lease_epoch"],
            phase="mutation_started",
            lease_seconds=10,
            now=NOW + timedelta(seconds=1),
        )
        fixture.runtime.upload_jobs.terminate_upload_job(job["job_id"])
        fixture.runtime.attempts.expire(now=NOW + timedelta(seconds=11))

        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["status"]
            == "reconciling"
        )
        terminated = fixture.runtime.upload_jobs.terminate_upload_job(job["job_id"])
        assert terminated["status"] == "reconciling"
        assert terminated["abort_requested"] is True
        reconcile = fixture.runtime.attempts.claim(
            client_id="extension-abort-reconcile",
            connected_platforms=["qidian"],
            now=NOW + timedelta(seconds=12),
        )
        assert reconcile is not None
        assert reconcile["execution_mode"] == "reconcile"
        assert reconcile["abort_requested"] is True
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=reconcile["attempt_id"],
            worker_id="extension-abort-reconcile",
            lease_epoch=reconcile["lease_epoch"],
            phase="observation_started",
            now=NOW + timedelta(seconds=12),
        )
        resolved = fixture.runtime.upload_jobs.reconcile_upload_job(
            job_id=job["job_id"],
            client_id="extension-abort-reconcile",
            attempt_id=reconcile["attempt_id"],
            lease_epoch=reconcile["lease_epoch"],
            outcome="absent",
            receipt=None,
            evidence={"pagination_complete": True},
            now=NOW + timedelta(seconds=13),
        )

        assert resolved["status"] == "cancelled"
    finally:
        fixture.engine.dispose()


def test_restart_moves_even_pre_mutation_running_job_to_reconciling() -> None:
    fixture = _fixture("publisher-attempt-restart-uncertain")
    try:
        job = _released_job(fixture)
        fixture.runtime.attempts.claim(
            client_id="extension-restart",
            connected_platforms=["qidian"],
            lease_seconds=90,
            now=NOW,
        )

        recovered = fixture.runtime.attempts.recover_interrupted(
            now=NOW + timedelta(seconds=1)
        )

        assert recovered == [job["job_id"]]
        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["status"]
            == "reconciling"
        )
    finally:
        fixture.engine.dispose()


def test_pre_mutation_failures_are_not_capped_at_three_attempts() -> None:
    fixture = _fixture("publisher-attempt-no-three-cap")
    try:
        job = _released_job(fixture)
        for attempt_number in range(1, 5):
            claim_time = NOW + timedelta(minutes=attempt_number)
            claimed = fixture.runtime.attempts.claim(
                client_id="extension-retry",
                connected_platforms=["qidian"],
                lease_seconds=30,
                now=claim_time,
            )
            assert claimed is not None
            assert claimed["attempt_number"] == attempt_number
            updated = fixture.runtime.upload_jobs.update_upload_job_result(
                job_id=job["job_id"],
                client_id="extension-retry",
                attempt_id=claimed["attempt_id"],
                lease_epoch=claimed["lease_epoch"],
                outcome="failed",
                message="pre-mutation transport failure",
                current_url="",
                error_code="offline",
                error_message="offline",
                details={},
                now=claim_time + timedelta(seconds=1),
            )
            assert updated["status"] == "pending"

        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadAttempt.id))) == 4
        assert (
            fixture.runtime.upload_jobs.get_upload_job(job["job_id"])["status"]
            == "pending"
        )
    finally:
        fixture.engine.dispose()
