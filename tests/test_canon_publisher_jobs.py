from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

from forwin.candidate_drafts import candidate_body_hash
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.audit import DecisionEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import PublisherUploadJob
from forwin.publisher_runtime.service import PublisherRuntimeService
from tests.postgres import postgres_test_url


@dataclass(frozen=True)
class CanonFixture:
    engine: object
    runtime: PublisherRuntimeService
    project_id: str
    chapter_plan_id: str
    candidate_id: str
    canon_commit_id: str
    canon_idempotency_key: str
    chapter_number: int
    chapter_title: str
    body: str


def _fixture(name: str) -> CanonFixture:
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    session_factory = get_session_factory(engine)
    project_id = new_id()
    chapter_plan_id = new_id()
    candidate_id = new_id()
    canon_commit_id = new_id()
    canon_idempotency_key = f"canon:{name}"
    chapter_number = 7
    chapter_title = "第七章 旧城回声"
    body = "雨停以后，旧城的钟声沿着空巷传来。"
    with session_factory.begin() as session:
        session.add(
            Project(
                id=project_id,
                title="发布身份测试",
                premise="验证 Canon 发布身份。",
                genre="悬疑",
                automation_json=json.dumps(
                    {
                        "publish_bindings": [
                            {"platform": "qidian", "book_name": "事件时书名"}
                        ]
                    }
                ),
            )
        )
        session.flush()
        arc = ArcPlanVersion(
            id=new_id(),
            project_id=project_id,
            version=1,
            arc_synopsis="第一幕",
            status="active",
        )
        session.add(arc)
        session.flush()
        session.add(
            ChapterPlan(
                id=chapter_plan_id,
                project_id=project_id,
                arc_plan_id=arc.id,
                chapter_number=chapter_number,
                title=chapter_title,
                one_line="回到旧城",
                goals_json="[]",
                status="accepted",
            )
        )
        session.flush()
        draft = ChapterDraft(
            id=new_id(),
            chapter_plan_id=chapter_plan_id,
            version=1,
            body_text=body,
            summary="主角听见旧城钟声。",
            char_count=len(body),
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(
            id=new_id(),
            draft_id=draft.id,
            verdict="pass",
            issues_json="[]",
            review_meta_json='{"verdict":"pass"}',
        )
        session.add(review)
        session.flush()
        session.add(
            CandidateDraftRecord(
                id=candidate_id,
                project_id=project_id,
                chapter_plan_id=chapter_plan_id,
                chapter_number=chapter_number,
                candidate_draft_id=draft.id,
                review_id=review.id,
                body_hash=candidate_body_hash(body),
                plan_revision="plan-r1",
                policy_version=1,
                status="accepted",
                canon_status="committed",
                canon_commit_id=canon_commit_id,
                idempotency_key=canon_idempotency_key,
            )
        )
        session.flush()
        session.add(
            CanonCommitRecord(
                id=canon_commit_id,
                idempotency_key=canon_idempotency_key,
                candidate_id=candidate_id,
                project_id=project_id,
                chapter_number=chapter_number,
                status="committed",
            )
        )
    runtime = PublisherRuntimeService(
        session_factory=session_factory,
        extension_api_key="secret",
        heartbeat_stale_seconds=90,
        preferred_client_id="",
        publisher_session_secret="",
        publisher_session_encryption_required=False,
    )
    return CanonFixture(
        engine=engine,
        runtime=runtime,
        project_id=project_id,
        chapter_plan_id=chapter_plan_id,
        candidate_id=candidate_id,
        canon_commit_id=canon_commit_id,
        canon_idempotency_key=canon_idempotency_key,
        chapter_number=chapter_number,
        chapter_title=chapter_title,
        body=body,
    )


def _materialize(
    fixture: CanonFixture,
    *,
    bindings: list[dict] | None = None,
    chapter_title: str | None = None,
    publish: bool = True,
) -> list[dict]:
    return fixture.runtime.canon_jobs.materialize(
        canon_commit_id=fixture.canon_commit_id,
        canon_idempotency_key=fixture.canon_idempotency_key,
        project_id=fixture.project_id,
        chapter_number=fixture.chapter_number,
        candidate_id=fixture.candidate_id,
        chapter_title=chapter_title or fixture.chapter_title,
        bindings=bindings
        or [
            {
                "platform": "qidian",
                "book_name": "事件时书名",
                "upload_url": "https://write.example/old",
                "create_if_missing": False,
                "publisher_compliance_required": False,
                "book_meta": {"audience": "male"},
            }
        ],
        publish=publish,
    )


def test_same_canon_event_replay_materializes_one_scheduled_job() -> None:
    fixture = _fixture("canon-publisher-replay")
    try:
        first = _materialize(fixture)
        second = _materialize(fixture)

        assert [item["job_id"] for item in second] == [item["job_id"] for item in first]
        assert first[0]["status"] == "scheduled"
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadJob.id))) == 1
    finally:
        fixture.engine.dispose()


def test_two_platform_snapshots_materialize_two_exact_jobs() -> None:
    fixture = _fixture("canon-publisher-platforms")
    try:
        jobs = _materialize(
            fixture,
            bindings=[
                {"platform": "qidian", "book_name": "起点版"},
                {"platform": "fanqie", "book_name": "番茄版"},
            ],
        )

        assert {job["platform"] for job in jobs} == {"qidian", "fanqie"}
        assert len({job["result_payload"]["publisher_identity"] for job in jobs}) == 2
    finally:
        fixture.engine.dispose()


def test_duplicate_platform_in_event_snapshot_is_rejected_atomically() -> None:
    fixture = _fixture("canon-publisher-duplicate-platform")
    try:
        with pytest.raises(ValueError, match="duplicate publisher platform"):
            _materialize(
                fixture,
                bindings=[
                    {"platform": "qidian", "book_name": "主绑定"},
                    {"platform": "qidian", "book_name": "重复绑定"},
                ],
            )

        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadJob.id))) == 0
    finally:
        fixture.engine.dispose()


def test_replay_uses_event_snapshot_after_project_settings_change() -> None:
    fixture = _fixture("canon-publisher-settings-snapshot")
    try:
        first = _materialize(fixture)
        with fixture.runtime.session_factory.begin() as session:
            project = session.get(Project, fixture.project_id)
            assert project is not None
            project.automation_json = json.dumps(
                {
                    "publish_bindings": [
                        {"platform": "fanqie", "book_name": "后来改的书名"}
                    ]
                }
            )

        replay = _materialize(fixture)

        assert replay[0]["job_id"] == first[0]["job_id"]
        assert replay[0]["book_name"] == "事件时书名"
        assert replay[0]["upload_url"] == "https://write.example/old"
    finally:
        fixture.engine.dispose()


def test_chapter_title_edit_cannot_create_a_second_identity() -> None:
    fixture = _fixture("canon-publisher-title-change")
    try:
        created = _materialize(fixture)
        with fixture.runtime.session_factory.begin() as session:
            chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
            assert chapter is not None
            chapter.title = "第七章 后改标题"

        replay = _materialize(fixture)

        assert replay[0]["job_id"] == created[0]["job_id"]
        assert replay[0]["chapter_title"] == fixture.chapter_title
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadJob.id))) == 1
    finally:
        fixture.engine.dispose()


def test_mismatched_replay_payload_fails_without_overwriting_job() -> None:
    fixture = _fixture("canon-publisher-mismatch")
    try:
        created = _materialize(fixture)

        with pytest.raises(ValueError, match="immutable publisher job mismatch"):
            _materialize(
                fixture,
                bindings=[
                    {
                        "platform": "qidian",
                        "book_name": "被篡改的书名",
                        "upload_url": "https://write.example/old",
                    }
                ],
            )

        stored = fixture.runtime.upload_jobs.get_upload_job(created[0]["job_id"])
        assert stored["book_name"] == "事件时书名"
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadJob.id))) == 1
    finally:
        fixture.engine.dispose()


def test_blocked_preflight_keeps_durable_scheduled_job() -> None:
    fixture = _fixture("canon-publisher-blocked-preflight")

    class BlockingPreflight:
        @staticmethod
        def check_upload_readiness(**_request):
            return {
                "ok": False,
                "blocking": [{"code": "missing_metadata", "message": "缺少元数据"}],
                "warnings": [],
                "platform_meta": {},
            }

    fixture.runtime.upload_jobs.preflight = BlockingPreflight()
    try:
        created = _materialize(fixture)

        assert created[0]["status"] == "scheduled"
        released = fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[created[0]["job_id"]],
            publish=True,
            actor_type="scheduler",
        )

        assert released == []
        stored = fixture.runtime.upload_jobs.get_upload_job(created[0]["job_id"])
        assert stored["status"] == "scheduled"
        assert stored["result_payload"]["preflight"]["ok"] is False
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadJob.id))) == 1
    finally:
        fixture.engine.dispose()


def test_publish_mode_changes_before_claim_and_freezes_after_claim() -> None:
    fixture = _fixture("canon-publisher-mode-freeze")
    try:
        created = _materialize(fixture, publish=False)
        released = fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[created[0]["job_id"]],
            publish=False,
            actor_type="scheduler",
        )
        assert released[0]["status"] == "pending"

        fixture.runtime.canon_jobs.release(
            project_id=fixture.project_id,
            job_ids=[created[0]["job_id"]],
            publish=True,
            actor_type="manual_ui",
        )
        before_claim = fixture.runtime.upload_jobs.get_upload_job(created[0]["job_id"])
        assert before_claim["publish"] is True

        claimed = fixture.runtime.upload_jobs.claim_next_upload_job(
            client_id="client-mode-freeze",
            connected_platforms=["qidian"],
        )
        assert claimed is not None
        with pytest.raises(ValueError, match="frozen after first claim"):
            fixture.runtime.canon_jobs.release(
                project_id=fixture.project_id,
                job_ids=[created[0]["job_id"]],
                publish=False,
                actor_type="manual_ui",
            )

        with fixture.runtime.session_factory() as session:
            events = (
                session.execute(
                    select(DecisionEvent).where(
                        DecisionEvent.project_id == fixture.project_id
                    )
                )
                .scalars()
                .all()
            )
        assert any(
            json.loads(event.payload_json).get("publish_mode_to") is True
            for event in events
        )
    finally:
        fixture.engine.dispose()


def test_external_release_session_can_roll_back_job_and_audit() -> None:
    fixture = _fixture("canon-publisher-release-rollback")
    try:
        created = _materialize(fixture)
        session = fixture.runtime.session_factory()
        try:
            fixture.runtime.canon_jobs.release(
                project_id=fixture.project_id,
                job_ids=[created[0]["job_id"]],
                publish=True,
                actor_type="scheduler",
                session=session,
            )
            session.rollback()
        finally:
            session.close()

        stored = fixture.runtime.upload_jobs.get_upload_job(created[0]["job_id"])
        assert stored["status"] == "scheduled"
    finally:
        fixture.engine.dispose()
