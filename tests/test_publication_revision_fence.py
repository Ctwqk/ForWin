from datetime import timedelta

import pytest
from sqlalchemy import select

from forwin.models.canon import CanonPublicationProtection
from forwin.models.project import ChapterPlan
from tests.test_canon_publisher_jobs import _fixture, _materialize
from tests.test_publisher_attempt_leases import NOW, _released_job


def test_release_rejects_stale_queued_canon():
    fixture = _fixture("stale-canon-release")
    try:
        jobs = _materialize(fixture)
        with fixture.runtime.session_factory.begin() as session:
            session.get(ChapterPlan, fixture.chapter_plan_id).active_commit_id = None
        with pytest.raises(ValueError, match="stale Canon"):
            fixture.runtime.canon_jobs.release(
                project_id=fixture.project_id,
                job_ids=[jobs[0]["job_id"]],
                publish=True,
                actor_type="system",
            )
    finally:
        fixture.engine.dispose()


def test_mutation_authorization_persists_protection_before_external_action():
    fixture = _fixture("publication-reservation")
    try:
        _released_job(fixture)
        claim = fixture.runtime.attempts.claim(
            client_id="revision-worker", connected_platforms=["qidian"], now=NOW
        )
        fixture.runtime.attempts.transition(
            job_id=claim["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id="revision-worker",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        with fixture.runtime.session_factory() as session:
            protection = session.scalar(select(CanonPublicationProtection))
            assert protection is not None, (
                "external action must first persist independent protection"
            )
            assert protection.canon_commit_id == fixture.canon_commit_id
            assert protection.state == "reserved"
    finally:
        fixture.engine.dispose()


def test_submitted_binding_protects_chapter_after_job_deletion():
    from sqlalchemy import delete

    from forwin.models.publisher import (
        PublisherChapterBinding,
        PublisherUploadJob,
        PublisherWorkBinding,
    )
    from forwin.publisher_runtime.bindings import _normalize_publish_state
    from forwin.publisher_runtime.protection import require_revision_unprotected

    fixture = _fixture("submitted-binding-protection")
    try:
        jobs = _materialize(fixture)
        with fixture.runtime.session_factory.begin() as session:
            job = session.get(PublisherUploadJob, jobs[0]["job_id"])
            job.publish = True
            state = _normalize_publish_state(job, {"official_state": "review_pending"})
            assert state == "submitted"
            session.add(
                PublisherWorkBinding(
                    id="submitted-work",
                    project_id=fixture.project_id,
                    platform_id="qidian",
                )
            )
            session.flush()
            session.add(
                PublisherChapterBinding(
                    work_binding_id="submitted-work",
                    project_id=fixture.project_id,
                    platform_id="qidian",
                    chapter_number=fixture.chapter_number,
                    publish_state=state,
                )
            )
            session.execute(delete(PublisherUploadJob))
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(CanonPublicationProtection)) is None
            with pytest.raises(ValueError, match="publication protection"):
                require_revision_unprotected(
                    session,
                    project_id=fixture.project_id,
                    from_chapter=fixture.chapter_number,
                )
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("state", ["published", "drafted", "review_pending"])
def test_confirmed_remote_content_stays_protected_after_local_cleanup(state):
    from sqlalchemy import delete

    from forwin.candidate_drafts import candidate_body_hash
    from forwin.models.publisher import (
        PublisherChapterBinding,
        PublisherUploadAttempt,
        PublisherUploadJob,
        PublisherUploadReceipt,
    )
    from forwin.publisher_runtime.protection import require_revision_unprotected

    fixture = _fixture("publication-cleanup-" + state)
    try:
        _released_job(fixture)
        claim = fixture.runtime.attempts.claim(
            client_id="cleanup-worker", connected_platforms=["qidian"], now=NOW
        )
        fixture.runtime.attempts.transition(
            job_id=claim["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id="cleanup-worker",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        body_hash = candidate_body_hash(fixture.body)
        fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=claim["job_id"],
            attempt_id=claim["attempt_id"],
            client_id="cleanup-worker",
            lease_epoch=claim["lease_epoch"],
            receipt={
                "remote_book_id": "book-1",
                "remote_chapter_id": "remote-7",
                "official_state": state,
                "content_sha256": body_hash,
                "evidence": {"content_sha256": body_hash},
            },
            now=NOW + timedelta(seconds=2),
        )
        with fixture.runtime.session_factory.begin() as session:
            session.execute(delete(PublisherChapterBinding))
            session.execute(delete(PublisherUploadReceipt))
            session.execute(delete(PublisherUploadAttempt))
            session.execute(delete(PublisherUploadJob))
        with fixture.runtime.session_factory() as session:
            with pytest.raises(ValueError, match="publication protection"):
                require_revision_unprotected(
                    session,
                    project_id=fixture.project_id,
                    from_chapter=fixture.chapter_number,
                )
            protection = session.scalar(select(CanonPublicationProtection))
            assert protection.state == (
                "published" if state == "published" else "reserved"
            )
    finally:
        fixture.engine.dispose()


def test_revision_owner_lock_serializes_external_mutation_authorization():
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from forwin.publisher_runtime.protection import lock_project_chapters

    fixture = _fixture("publication-race")
    try:
        _released_job(fixture)
        claim = fixture.runtime.attempts.claim(
            client_id="race-worker", connected_platforms=["qidian"], now=NOW
        )
        entered = Event()

        def mutate():
            entered.set()
            return fixture.runtime.attempts.transition(
                job_id=claim["job_id"],
                attempt_id=claim["attempt_id"],
                worker_id="race-worker",
                lease_epoch=claim["lease_epoch"],
                phase="mutation_started",
                now=NOW + timedelta(seconds=1),
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            with fixture.runtime.session_factory.begin() as session:
                lock_project_chapters(session, fixture.project_id)
                future = executor.submit(mutate)
                assert entered.wait(timeout=2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.2)
                # Represents a successful mainline switch before the publisher owns the lock.
                session.get(
                    ChapterPlan, fixture.chapter_plan_id
                ).active_commit_id = None
            with pytest.raises(ValueError, match="stale Canon"):
                future.result(timeout=5)
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(CanonPublicationProtection)) is None
    finally:
        fixture.engine.dispose()


def test_first_publisher_job_cannot_change_accepted_title():
    fixture = _fixture("publication-title-fence")
    try:
        with pytest.raises(ValueError, match="title"):
            _materialize(fixture, chapter_title="改写后的读者可见标题")
    finally:
        fixture.engine.dispose()


def test_out_of_order_publication_releases_client_until_prefix_is_confirmed():
    from forwin.candidate_drafts import candidate_body_hash
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
    from forwin.models.publisher import PublisherUploadAttempt, PublisherUploadJob

    fixture = _fixture("publication-prefix-real", chapter_number=2)
    try:
        _released_job(fixture)
        second = fixture.runtime.attempts.claim(
            client_id="serial-worker", connected_platforms=["qidian"], now=NOW
        )
        second_action = {
            "job_id": second["job_id"],
            "attempt_id": second["attempt_id"],
            "worker_id": "serial-worker",
            "lease_epoch": second["lease_epoch"],
            "phase": "mutation_started",
            "now": NOW + timedelta(seconds=1),
        }
        with pytest.raises(ValueError, match="publication prefix"):
            fixture.runtime.attempts.transition(**second_action)
        with fixture.runtime.session_factory() as session:
            waiting = session.get(PublisherUploadJob, second["job_id"])
            assert waiting.status == "pending"
            assert waiting.current_attempt_id == ""
            assert waiting.available_at is not None
            assert (
                session.get(PublisherUploadAttempt, second["attempt_id"]).status
                == "cancelled"
            )
            assert session.scalar(select(CanonPublicationProtection)) is None
        with fixture.runtime.session_factory.begin() as session:
            arc_id = session.get(ChapterPlan, fixture.chapter_plan_id).arc_plan_id
            first = ChapterPlan(
                id="first-plan",
                project_id=fixture.project_id,
                arc_plan_id=arc_id,
                chapter_number=1,
                title="第一章",
                status="accepted",
            )
            session.add(first)
            session.flush()
            session.add(
                ChapterDraft(
                    id="first-draft",
                    chapter_plan_id=first.id,
                    version=1,
                    body_text="First body",
                )
            )
            session.flush()
            session.add(
                ChapterReview(id="first-review", draft_id="first-draft", verdict="pass")
            )
            session.flush()
            session.add(
                CandidateDraftRecord(
                    id="first-candidate",
                    project_id=fixture.project_id,
                    chapter_plan_id=first.id,
                    chapter_number=1,
                    candidate_draft_id="first-draft",
                    review_id="first-review",
                    status="accepted",
                    canon_commit_id="first-commit",
                    idempotency_key="first-key",
                    body_hash=candidate_body_hash("First body"),
                )
            )
            session.flush()
            session.add(
                CanonCommitRecord(
                    id="first-commit",
                    candidate_id="first-candidate",
                    project_id=fixture.project_id,
                    chapter_plan_id=first.id,
                    chapter_number=1,
                    chapter_title="第一章",
                    idempotency_key="first-key",
                )
            )
            session.flush()
            first.active_commit_id = "first-commit"
        first_jobs = fixture.runtime.canon_jobs.materialize(
            canon_commit_id="first-commit",
            canon_idempotency_key="first-key",
            project_id=fixture.project_id,
            chapter_number=1,
            candidate_id="first-candidate",
            chapter_title="第一章",
            body_sha256=candidate_body_hash("First body"),
            bindings=[{"platform": "qidian", "book_name": "book"}],
        )
        fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[first_jobs[0]["job_id"]],
            publish=True,
            actor_type="system",
        )
        first_claim = fixture.runtime.attempts.claim(
            client_id="serial-worker",
            connected_platforms=["qidian"],
            now=NOW + timedelta(seconds=2),
        )
        assert first_claim["job_id"] == first_jobs[0]["job_id"]
        fixture.runtime.attempts.transition(
            job_id=first_claim["job_id"],
            attempt_id=first_claim["attempt_id"],
            worker_id="serial-worker",
            lease_epoch=first_claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=3),
        )
        # An unresolved external first-chapter mutation still cannot authorize chapter two.
        still_waiting = fixture.runtime.attempts.claim(
            client_id="waiting-worker",
            connected_platforms=["qidian"],
            now=NOW + timedelta(seconds=32),
        )
        with pytest.raises(ValueError, match="publication prefix"):
            fixture.runtime.attempts.transition(
                job_id=still_waiting["job_id"],
                attempt_id=still_waiting["attempt_id"],
                worker_id="waiting-worker",
                lease_epoch=still_waiting["lease_epoch"],
                phase="mutation_started",
                now=NOW + timedelta(seconds=33),
            )
        with fixture.runtime.session_factory() as session:
            protected = session.scalar(select(CanonPublicationProtection))
            assert protected.upload_job_id == first_claim["job_id"]
            assert protected.state == "reserved"
            assert (
                session.get(PublisherUploadAttempt, first_claim["attempt_id"]).status
                == "running"
            )
        body_hash = candidate_body_hash("First body")
        fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=first_claim["job_id"],
            attempt_id=first_claim["attempt_id"],
            client_id="serial-worker",
            lease_epoch=first_claim["lease_epoch"],
            now=NOW + timedelta(seconds=34),
            receipt={
                "remote_book_id": "book",
                "remote_chapter_id": "chapter1",
                "official_state": "published",
                "content_sha256": body_hash,
                "evidence": {"content_sha256": body_hash},
            },
        )
        fixture.runtime.upload_jobs.update_upload_job_result(
            job_id=first_claim["job_id"],
            attempt_id=first_claim["attempt_id"],
            client_id="serial-worker",
            lease_epoch=first_claim["lease_epoch"],
            outcome="succeeded",
            message="published",
            current_url="",
            error_code="",
            error_message="",
            now=NOW + timedelta(seconds=35),
        )
        resumed = fixture.runtime.attempts.claim(
            client_id="serial-worker",
            connected_platforms=["qidian"],
            now=NOW + timedelta(seconds=64),
        )
        assert resumed["job_id"] == second["job_id"]
        fixture.runtime.attempts.transition(
            job_id=resumed["job_id"],
            attempt_id=resumed["attempt_id"],
            worker_id="serial-worker",
            lease_epoch=resumed["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=65),
        )
        with fixture.runtime.session_factory() as session:
            assert (
                session.get(PublisherUploadAttempt, resumed["attempt_id"]).phase
                == "mutation_started"
            )
    finally:
        fixture.engine.dispose()


def test_out_of_order_legacy_receipt_still_records_permanent_public_fact():
    from forwin.candidate_drafts import candidate_body_hash
    from forwin.models.publisher import PublisherUploadAttempt

    fixture = _fixture("legacy-publication-hole", chapter_number=2)
    try:
        _released_job(fixture)
        claim = fixture.runtime.attempts.claim(
            client_id="legacy-worker", connected_platforms=["qidian"], now=NOW
        )
        with fixture.runtime.session_factory.begin() as session:
            # Persisted action from an older runtime predates the prefix guard.
            session.get(
                PublisherUploadAttempt, claim["attempt_id"]
            ).phase = "mutation_started"
        body_hash = candidate_body_hash(fixture.body)
        fixture.runtime.upload_jobs.record_upload_receipt(
            job_id=claim["job_id"],
            attempt_id=claim["attempt_id"],
            client_id="legacy-worker",
            lease_epoch=claim["lease_epoch"],
            now=NOW + timedelta(seconds=1),
            receipt={
                "remote_book_id": "book",
                "remote_chapter_id": "out-of-order-chapter2",
                "official_state": "published",
                "content_sha256": body_hash,
                "evidence": {"content_sha256": body_hash},
            },
        )
        with fixture.runtime.session_factory() as session:
            fact = session.scalar(select(CanonPublicationProtection))
            assert fact.chapter_number == 2
            assert fact.state == "published"
    finally:
        fixture.engine.dispose()


def test_stale_pre_action_job_is_cancelled_without_starving_next_work():
    from forwin.candidate_drafts import candidate_body_hash
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.publisher import PublisherUploadJob

    fixture = _fixture("stale-claim-does-not-starve")
    try:
        old = _released_job(fixture)
        with fixture.runtime.session_factory.begin() as session:
            session.get(PublisherUploadJob, old["job_id"]).created_at = NOW
            session.add(
                CanonCommitRecord(
                    id="replacement",
                    idempotency_key="replacement-key",
                    candidate_id=fixture.candidate_id,
                    project_id=fixture.project_id,
                    chapter_plan_id=fixture.chapter_plan_id,
                    chapter_number=1,
                    chapter_title=fixture.chapter_title,
                    acceptance_revision=2,
                )
            )
            session.flush()
            session.get(
                ChapterPlan, fixture.chapter_plan_id
            ).active_commit_id = "replacement"
            session.add(
                PublisherUploadJob(
                    id="healthy-job",
                    idempotency_key="healthy-job-key",
                    project_id="",
                    platform_id="qidian",
                    book_name="Independent work",
                    body_text="body",
                    body_sha256=candidate_body_hash("body"),
                    status="pending",
                    created_at=NOW + timedelta(seconds=1),
                )
            )
        claim = fixture.runtime.attempts.claim(
            client_id="healthy-worker",
            connected_platforms=["qidian"],
            now=NOW + timedelta(seconds=2),
        )
        assert claim["job_id"] == "healthy-job"
        with fixture.runtime.session_factory() as session:
            stale = session.get(PublisherUploadJob, old["job_id"])
            assert stale.status == "cancelled"
            assert "stale Canon" in stale.error_message
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("state", ["reserved", "published"])
def test_stale_job_with_external_protection_gets_only_read_only_reconciliation(state):
    from forwin.models.publisher import PublisherUploadJob

    fixture = _fixture("stale-protected-claim-" + state)
    try:
        old = _released_job(fixture)
        with fixture.runtime.session_factory.begin() as session:
            job = session.get(PublisherUploadJob, old["job_id"])
            session.add(
                CanonPublicationProtection(
                    project_id=fixture.project_id,
                    chapter_plan_id=fixture.chapter_plan_id,
                    chapter_number=1,
                    canon_commit_id=fixture.canon_commit_id,
                    upload_job_id=job.id,
                    platform_id="qidian",
                    content_sha256=job.body_sha256,
                    state=state,
                )
            )
            # A migrated inconsistent queue must not discard external uncertainty.
            session.get(ChapterPlan, fixture.chapter_plan_id).active_commit_id = None
        claim = fixture.runtime.attempts.claim(
            client_id="reconcile-worker",
            connected_platforms=["qidian"],
            now=NOW,
        )
        assert claim["job_id"] == old["job_id"]
        assert claim["execution_mode"] == "reconcile"
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(CanonPublicationProtection.state)) == state
    finally:
        fixture.engine.dispose()


def test_conflicting_publication_is_paused_without_reclaiming_or_losing_protection():
    from forwin.candidate_drafts import candidate_body_hash
    from forwin.models.publisher import PublisherUploadAttempt, PublisherUploadJob
    from forwin.publisher_runtime.protection import PublicationConflictError

    fixture = _fixture("duplicate-publication-pause")
    try:
        original = _released_job(fixture)
        first = fixture.runtime.attempts.claim(
            client_id="first-worker",
            connected_platforms=["qidian"],
            now=NOW,
        )
        fixture.runtime.attempts.transition(
            job_id=first["job_id"],
            attempt_id=first["attempt_id"],
            worker_id="first-worker",
            lease_epoch=first["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        with fixture.runtime.session_factory.begin() as session:
            session.add(
                PublisherUploadJob(
                    id="duplicate-job",
                    idempotency_key="duplicate-key",
                    project_id=fixture.project_id,
                    platform_id="qidian",
                    canon_commit_id=fixture.canon_commit_id,
                    candidate_id=fixture.candidate_id,
                    chapter_number=1,
                    chapter_title=fixture.chapter_title,
                    body_text=fixture.body,
                    body_sha256=candidate_body_hash(fixture.body),
                    created_at=NOW + timedelta(seconds=2),
                    status="pending",
                )
            )
            session.add(
                PublisherUploadJob(
                    id="independent-job",
                    idempotency_key="independent-key",
                    project_id="",
                    platform_id="qidian",
                    body_text="body",
                    body_sha256=candidate_body_hash("body"),
                    status="pending",
                    created_at=NOW + timedelta(seconds=3),
                )
            )
        second = fixture.runtime.attempts.claim(
            client_id="second-worker",
            connected_platforms=["qidian"],
            now=NOW + timedelta(seconds=4),
        )
        assert second["job_id"] == "duplicate-job"
        with pytest.raises(PublicationConflictError):
            fixture.runtime.attempts.transition(
                job_id=second["job_id"],
                attempt_id=second["attempt_id"],
                worker_id="second-worker",
                lease_epoch=second["lease_epoch"],
                phase="mutation_started",
                now=NOW + timedelta(seconds=5),
            )
        with fixture.runtime.session_factory() as session:
            job = session.get(PublisherUploadJob, "duplicate-job")
            assert job.status == "paused"
            assert job.pause_reason == "publication_conflict"
            attempt = session.get(PublisherUploadAttempt, second["attempt_id"])
            assert attempt.status == "paused"
            assert attempt.phase == "claimed"
            protection = session.scalar(select(CanonPublicationProtection))
            assert protection.upload_job_id == original["job_id"]
            assert protection.state == "reserved"
        next_claim = fixture.runtime.attempts.claim(
            client_id="second-worker",
            connected_platforms=["qidian"],
            now=NOW + timedelta(seconds=6),
        )
        assert next_claim["job_id"] == "independent-job"
    finally:
        fixture.engine.dispose()
