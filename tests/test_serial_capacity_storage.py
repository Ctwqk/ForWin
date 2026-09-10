"""Real PostgreSQL contention and recovery tests for the serial capacity owner."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from forwin.generation.task_payload import execution_payload
from forwin.models.base import get_engine
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_test_url


@pytest.fixture
def capacity_db():
    from forwin.production.capacity import SerialCapacityService

    engine = get_engine(postgres_test_url("capacity"))
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            Project(
                id="p",
                title="Book",
                premise="Story",
                target_total_chapters=100,
                automation_json=json.dumps({"primary_publish_platform": "fanqie"}),
            )
        )
        session.flush()
        session.add(ArcPlanVersion(id="arc", project_id="p", arc_synopsis="arc"))
        session.flush()
        for number in range(1, 8):
            session.add(
                ChapterPlan(
                    id=f"ch{number}",
                    project_id="p",
                    arc_plan_id="arc",
                    chapter_number=number,
                    status="accepted" if number <= 4 else "planned",
                )
            )
        session.flush()
        for number in range(1, 5):
            accept_chapter(session, number)
        session.flush()
        SerialCapacityService(session).snapshot("p")
    yield factory
    engine.dispose()


def add_task(
    factory, task_id="t", *, mode="daily_serial", isolated=False, status="running"
):
    with factory.begin() as session:
        payload = execution_payload(
            mode="continue",
            policy=RuntimePolicy.for_profile("standard"),
            policy_version=1,
        )
        data = payload.model_dump()
        data.update(long_run_mode=mode, isolated=isolated)
        session.add(
            GenerationTask(
                id=task_id,
                project_id="p",
                status=status,
                task_kind="test" if task_id != "t" else "generation",
                lease_owner=task_id,
                lease_epoch=1,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                execution_payload_json=json.dumps(data),
            )
        )


def test_concurrent_workers_only_reserve_final_slot_and_retry_is_free(capacity_db):
    from forwin.production.capacity import CapacityWait, SerialCapacityService

    factory = capacity_db
    add_task(factory, "t")
    add_task(factory, "other")

    def reserve(task_id, chapter):
        try:
            with factory.begin() as session:
                SerialCapacityService(session).reserve(
                    "p", chapter, task_id=task_id, worker_id=task_id, lease_epoch=1
                )
            return True
        except CapacityWait:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda x: reserve(*x), [("t", 5), ("other", 5)]))
    assert sum(answers) == 1
    winner = ("t", 5) if answers[0] else ("other", 5)
    assert reserve(*winner)
    with factory() as session:
        snap = SerialCapacityService(session).snapshot("p")
        assert (snap.accepted, snap.reserved, snap.published, snap.available) == (
            4,
            1,
            0,
            0,
        )


def test_reclaimed_epoch_adopts_reservation_and_old_worker_cannot_release(capacity_db):
    from forwin.application.errors import GenerationTaskLeaseLost
    from forwin.production.capacity import SerialCapacityService

    factory = capacity_db
    add_task(factory)
    with factory.begin() as session:
        SerialCapacityService(session).reserve(
            "p", 5, task_id="t", worker_id="t", lease_epoch=1
        )
    with factory.begin() as session:
        session.get(GenerationTask, "t").lease_epoch = 2
    with factory.begin() as session:
        SerialCapacityService(session).reserve(
            "p", 5, task_id="t", worker_id="t", lease_epoch=2
        )
    with pytest.raises(GenerationTaskLeaseLost), factory.begin() as session:
        SerialCapacityService(session).release(
            "p", 5, task_id="t", worker_id="t", lease_epoch=1
        )
    with factory() as session:
        assert SerialCapacityService(session).snapshot("p").reserved == 1


def test_lowering_total_blocks_even_previously_reserved_commit_without_deleting_backlog(
    capacity_db,
):
    from forwin.production.capacity import CapacityWait, SerialCapacityService

    factory = capacity_db
    add_task(factory)
    with factory.begin() as session:
        SerialCapacityService(session).reserve(
            "p", 5, task_id="t", worker_id="t", lease_epoch=1
        )
    with factory.begin() as session:
        session.get(Project, "p").target_total_chapters = 60
    with pytest.raises(CapacityWait), factory.begin() as session:
        SerialCapacityService(session).validate_commit("p", 5)
    with factory.begin() as session:
        snap = SerialCapacityService(session).snapshot("p")
        assert (snap.limit, snap.accepted, snap.available, snap.config_version) == (
            3,
            4,
            0,
            2,
        )
        assert (
            len(
                session.scalars(
                    select(ChapterPlan).where(ChapterPlan.status == "accepted")
                ).all()
            )
            == 4
        )


@pytest.mark.parametrize("mode", ["factory_batch", "soak_test"])
def test_offline_exemption_requires_explicit_task_isolation(capacity_db, mode):
    from forwin.production.capacity import CapacityWait, SerialCapacityService

    factory = capacity_db
    add_task(factory, mode=mode, isolated=True)
    with factory.begin() as session:
        session.get(Project, "p").automation_json = "{}"
        SerialCapacityService(session).reserve(
            "p", 5, task_id="t", worker_id="t", lease_epoch=1
        )
        SerialCapacityService(session).validate_commit("p", 5)
        assert SerialCapacityService(session).commit_mode("p", 5) == mode
    with factory.begin() as session:
        task = session.get(GenerationTask, "t")
        data = json.loads(task.execution_payload_json)
        data["isolated"] = False
        task.execution_payload_json = json.dumps(data)
    with pytest.raises(CapacityWait), factory.begin() as session:
        SerialCapacityService(session).validate_commit("p", 5)


def test_unlinked_accepted_rows_do_not_fake_contiguous_canon(capacity_db):
    from forwin.production.capacity import SerialCapacityService

    with capacity_db.begin() as session:
        session.get(ChapterPlan, "ch2").active_commit_id = None
    with capacity_db.begin() as session:
        assert SerialCapacityService(session).snapshot("p").accepted == 1


def receipt(session, number, *, platform="fanqie", state="published", protect=True):
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.publisher import (
        PublisherUploadAttempt,
        PublisherUploadJob,
        PublisherUploadReceipt,
    )

    if session.get(CanonCommitRecord, f"commit{number}") is None:
        accept_chapter(session, number)
    key = f"{platform}-{number}"
    session.add(
        PublisherUploadJob(
            id=key,
            project_id="p",
            chapter_number=number,
            platform_id=platform,
            canon_commit_id=f"commit{number}",
            candidate_id=f"candidate{number}",
            body_text="body",
            body_sha256=__import__("hashlib").sha256(b"body").hexdigest(),
            status="succeeded",
        )
    )
    session.flush()
    session.add(
        PublisherUploadAttempt(
            id=key, upload_job_id=key, attempt_number=1, attempt_kind="upload"
        )
    )
    session.flush()
    session.add(
        PublisherUploadReceipt(
            upload_job_id=key,
            upload_attempt_id=key,
            receipt_key=key,
            platform_id=platform,
            official_state=state,
            content_sha256=__import__("hashlib").sha256(b"body").hexdigest(),
        )
    )
    session.flush()
    if protect:
        from forwin.publisher_runtime.protection import record_publication_receipt

        record_publication_receipt(
            session,
            session.get(PublisherUploadJob, key),
            state=state,
            remote_book_id="book",
            remote_chapter_id=str(number),
            evidence={"verified": True},
        )
    session.flush()


def test_confirmed_publication_holes_and_primary_platform_are_conservative(capacity_db):
    from forwin.production.capacity import SerialCapacityService

    with capacity_db.begin() as session:
        for n in [1, 2, 4, 5]:
            receipt(session, n)
        receipt(session, 3, state="review_pending")
        for n in range(1, 6):
            receipt(session, n, platform="qidian")
        assert SerialCapacityService(session).snapshot("p").published == 2
    with capacity_db.begin() as session:
        session.get(Project, "p").automation_json = json.dumps(
            {"primary_publish_platform": "qidian"}
        )
    with capacity_db.begin() as session:
        snap = SerialCapacityService(session).snapshot("p")
        assert (snap.published, snap.config_version) == (5, 2)


def test_capacity_wait_task_is_reclaimable_and_does_not_fail(capacity_db):
    from forwin.application.generation import GenerationApplicationService
    from forwin.config import InfrastructureConfig
    from forwin.generation.task_lease import claim_generation_task
    from forwin.production.capacity import SerialCapacityService

    factory = capacity_db
    add_task(factory)
    with factory.begin() as session:
        session.get(Project, "p").automation_json = "{}"
        task = session.get(GenerationTask, "t")
        task.status = "capacity_wait"
        task.current_stage = "capacity_wait"
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    service = GenerationApplicationService(
        session_factory=factory,
        infrastructure=InfrastructureConfig(),
        runner=lambda *args: pytest.fail(
            "must not construct writer while capacity blocked"
        ),
    )
    with factory.begin() as session:
        claim = claim_generation_task(session, worker_id="w")
    assert claim.task.id == "t"
    service.execute_claimed(
        claim.task, resume_from_chapter=5, worker_id="w", lease_epoch=claim.lease_epoch
    )
    with factory() as session:
        task = session.get(GenerationTask, "t")
        assert task.status == "capacity_wait"
        assert task.error_message == ""
        assert task.message == "primary_publish_platform_required"
    with factory.begin() as session:
        session.get(Project, "p").automation_json = json.dumps(
            {"primary_publish_platform": "fanqie"}
        )
        session.get(GenerationTask, "t").lease_expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    with factory.begin() as session:
        claim = claim_generation_task(session, worker_id="w2")
        SerialCapacityService(session).reserve(
            "p", 5, task_id="t", worker_id="w2", lease_epoch=claim.lease_epoch
        )
    assert claim.lease_epoch == 3


def accept_chapter(session, number):
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview

    session.add(
        ChapterDraft(id=f"d{number}", chapter_plan_id=f"ch{number}", body_text="body")
    )
    session.flush()
    session.add(ChapterReview(id=f"r{number}", draft_id=f"d{number}", verdict="pass"))
    session.flush()
    session.add(
        CandidateDraftRecord(
            id=f"candidate{number}",
            project_id="p",
            chapter_plan_id=f"ch{number}",
            chapter_number=number,
            candidate_draft_id=f"d{number}",
            review_id=f"r{number}",
            status="accepted",
            body_hash=__import__("hashlib").sha256(b"body").hexdigest(),
        )
    )
    session.flush()
    session.add(
        CanonCommitRecord(
            id=f"commit{number}",
            project_id="p",
            chapter_plan_id=f"ch{number}",
            chapter_number=number,
            candidate_id=f"candidate{number}",
            idempotency_key=f"commit{number}",
            status="committed",
        )
    )
    session.flush()
    session.get(ChapterPlan, f"ch{number}").active_commit_id = f"commit{number}"
    session.get(ChapterPlan, f"ch{number}").status = "accepted"
    session.flush()


def test_noncanon_receipt_cannot_create_serial_capacity(capacity_db):
    from forwin.models.publisher import PublisherUploadJob
    from forwin.production.capacity import SerialCapacityService

    with capacity_db.begin() as session:
        receipt(session, 1, protect=False)
        session.get(PublisherUploadJob, "fanqie-1").canon_commit_id = None
    with capacity_db.begin() as session:
        assert SerialCapacityService(session).snapshot("p").published == 0


def test_publication_progress_survives_job_and_receipt_deletion(capacity_db):
    from sqlalchemy import delete

    from forwin.models.publisher import (
        PublisherUploadAttempt,
        PublisherUploadJob,
        PublisherUploadReceipt,
    )
    from forwin.production.capacity import SerialCapacityService

    with capacity_db.begin() as session:
        receipt(session, 1)
        session.execute(delete(PublisherUploadReceipt))
        session.execute(delete(PublisherUploadAttempt))
        session.execute(delete(PublisherUploadJob))
    with capacity_db.begin() as session:
        assert SerialCapacityService(session).snapshot("p").published == 1


def test_revising_offline_accepted_chapter_cannot_grant_publication_mode(capacity_db):
    from forwin.models.canon import CanonCommitRecord
    from forwin.production.capacity import SerialCapacityService

    with capacity_db.begin() as session:
        session.get(CanonCommitRecord, "commit1").production_mode = "soak_test"
    with capacity_db.begin() as session:
        assert (
            SerialCapacityService(session).commit_mode(
                "p", 1, candidate_id="candidate1"
            )
            == "soak_test"
        )


def test_offline_needs_review_candidate_keeps_task_bound_mode(capacity_db):
    from forwin.models.draft import CandidateDraftRecord
    from forwin.production.capacity import SerialCapacityService

    factory = capacity_db
    add_task(factory, mode="factory_batch", isolated=True)
    with factory.begin() as session:
        SerialCapacityService(session).reserve(
            "p", 5, task_id="t", worker_id="t", lease_epoch=1
        )
        accept_chapter(session, 5)
        candidate = session.get(CandidateDraftRecord, "candidate5")
        candidate.metadata_json = json.dumps(
            SerialCapacityService(session).candidate_provenance("p", 5)
        )
        session.get(ChapterPlan, "ch5").active_commit_id = None
        session.get(ChapterPlan, "ch5").status = "needs_review"
        session.get(GenerationTask, "t").status = "needs_review"
        session.get(Project, "p").automation_json = "{}"
    with factory.begin() as session:
        capacity = SerialCapacityService(session)
        capacity.validate_commit("p", 5, candidate_id="candidate5")
        assert (
            capacity.commit_mode("p", 5, candidate_id="candidate5") == "factory_batch"
        )


def test_review_retry_transfers_same_slot_to_new_fenced_task(capacity_db):
    from forwin.models.capacity import ChapterCapacityReservation
    from forwin.production.capacity import SerialCapacityService

    add_task(capacity_db, mode="soak_test", isolated=True)
    with capacity_db.begin() as session:
        SerialCapacityService(session).reserve(
            "p", 5, task_id="t", worker_id="t", lease_epoch=1
        )
        session.get(GenerationTask, "t").status = "needs_review"
    add_task(capacity_db, "retry", mode="soak_test", isolated=True)
    with capacity_db.begin() as session:
        owner = SerialCapacityService(session)
        owner.reserve("p", 5, task_id="retry", worker_id="retry", lease_epoch=1)
        assert owner.snapshot("p").reserved == 1
        assert session.get(ChapterCapacityReservation, ("p", 5)).task_id == "retry"
        assert owner.candidate_provenance("p", 5) == {"generation_task_id": "retry"}


def test_failed_canon_transaction_keeps_reservation_and_accepted_prefix(capacity_db):
    from forwin.production.capacity import SerialCapacityService

    add_task(capacity_db)
    with capacity_db.begin() as session:
        SerialCapacityService(session).reserve(
            "p", 5, task_id="t", worker_id="t", lease_epoch=1
        )
    with pytest.raises(RuntimeError, match="injected"), capacity_db.begin() as session:
        owner = SerialCapacityService(session)
        owner.validate_commit("p", 5)
        accept_chapter(session, 5)
        owner.consume_commit("p", 5)
        raise RuntimeError("injected")
    with capacity_db.begin() as session:
        snapshot = SerialCapacityService(session).snapshot("p")
        assert (snapshot.accepted, snapshot.reserved, snapshot.available) == (4, 1, 0)
