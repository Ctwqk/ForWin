from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from forwin.publisher_runtime.service import PublisherRuntimeService
from tests.postgres import postgres_test_url
from tests.test_publisher_runtime_upload_jobs import seed_accepted_upload_identity


def _session_factory(name: str):
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, get_session_factory(engine)


def _runtime(name: str) -> tuple[object, PublisherRuntimeService]:
    engine, Session = _session_factory(name)
    return engine, PublisherRuntimeService(
        session_factory=Session,
        extension_api_key="secret",
        heartbeat_stale_seconds=90,
        preferred_client_id="",
        publisher_session_secret="",
        publisher_session_encryption_required=False,
    )


def _project(session, *, title: str = "发布测试") -> str:
    project_id = new_id()
    session.add(
        Project(
            id=project_id,
            title=title,
            premise="测试 premise",
            genre="玄幻",
            setting_summary="",
        )
    )
    return project_id


def _complete_upload(
    runtime: PublisherRuntimeService,
    created: dict,
    *,
    current_url: str,
    result_payload: dict,
) -> dict:
    seed_accepted_upload_identity(runtime, created)
    claimed = runtime.upload_jobs.claim_next_upload_job(
        client_id="client-1",
        connected_platforms=[created["platform"]],
    )
    assert claimed is not None
    assert claimed["job_id"] == created["job_id"]
    runtime.attempts.transition(
        job_id=created["job_id"],
        attempt_id=claimed["attempt_id"],
        worker_id="client-1",
        lease_epoch=claimed["lease_epoch"],
        phase="mutation_started",
        current_url=current_url,
    )
    runtime.upload_jobs.record_upload_receipt(
        job_id=created["job_id"],
        client_id="client-1",
        attempt_id=claimed["attempt_id"],
        lease_epoch=claimed["lease_epoch"],
        receipt={
            "remote_book_id": str(
                result_payload.get("remote_book_id")
                or result_payload.get("work_id")
                or ""
            ),
            "remote_chapter_id": str(result_payload.get("remote_chapter_id") or ""),
            "remote_url": str(result_payload.get("remote_book_url") or current_url),
            "official_state": str(result_payload.get("official_status") or "drafted"),
            "content_sha256": created["body_sha256"],
            "evidence": {
                "verified": True,
                "content_sha256": created["body_sha256"],
            },
        },
    )
    return runtime.upload_jobs.update_upload_job_result(
        job_id=created["job_id"],
        client_id="client-1",
        attempt_id=claimed["attempt_id"],
        lease_epoch=claimed["lease_epoch"],
        outcome="succeeded",
        message="完成",
        current_url=current_url,
        error_code="",
        error_message="",
        details={},
    )


def test_init_db_creates_publisher_binding_tables() -> None:
    engine, Session = _session_factory("publisher-binding-tables")
    try:
        with Session.begin() as session:
            project_id = _project(session)
            work = PublisherWorkBinding(
                project_id=project_id,
                platform_id="qidian",
                book_name="发布测试",
                remote_book_id="book-1",
                raw_payload_json=json.dumps({"remote_book_id": "book-1"}),
            )
            session.add(work)
            session.flush()
            session.add(
                PublisherChapterBinding(
                    work_binding_id=work.id,
                    project_id=project_id,
                    platform_id="qidian",
                    chapter_number=1,
                    chapter_title="第一章",
                    remote_chapter_id="chapter-1",
                )
            )

        with Session() as session:
            stored = session.execute(select(PublisherWorkBinding)).scalar_one()
            assert stored.remote_book_id == "book-1"
            chapter = session.execute(select(PublisherChapterBinding)).scalar_one()
            assert chapter.work_binding_id == stored.id
    finally:
        engine.dispose()


def test_work_binding_unique_per_project_platform() -> None:
    engine, Session = _session_factory("publisher-work-binding-unique")
    try:
        with pytest.raises(IntegrityError):
            with Session.begin() as session:
                project_id = _project(session)
                session.add_all(
                    [
                        PublisherWorkBinding(
                            project_id=project_id,
                            platform_id="fanqie",
                            book_name="同一本书",
                        ),
                        PublisherWorkBinding(
                            project_id=project_id,
                            platform_id="fanqie",
                            book_name="同一本书",
                        ),
                    ]
                )
    finally:
        engine.dispose()


def test_chapter_binding_unique_per_work_and_chapter_number() -> None:
    engine, Session = _session_factory("publisher-chapter-binding-unique")
    try:
        with pytest.raises(IntegrityError):
            with Session.begin() as session:
                project_id = _project(session)
                work = PublisherWorkBinding(
                    project_id=project_id,
                    platform_id="qidian",
                    book_name="章节唯一",
                )
                session.add(work)
                session.flush()
                session.add_all(
                    [
                        PublisherChapterBinding(
                            work_binding_id=work.id,
                            project_id=project_id,
                            platform_id="qidian",
                            chapter_number=1,
                            chapter_title="第一章",
                        ),
                        PublisherChapterBinding(
                            work_binding_id=work.id,
                            project_id=project_id,
                            platform_id="qidian",
                            chapter_number=1,
                            chapter_title="第一章 重试",
                        ),
                    ]
                )
    finally:
        engine.dispose()


def test_cover_asset_can_be_selected_for_work_binding() -> None:
    engine, Session = _session_factory("publisher-cover-selected")
    try:
        with Session.begin() as session:
            project_id = _project(session)
            work = PublisherWorkBinding(
                project_id=project_id,
                platform_id="fanqie",
                book_name="封面测试",
            )
            session.add(work)
            session.flush()
            cover = PublisherCoverAsset(
                project_id=project_id,
                work_binding_id=work.id,
                source="minimax",
                prompt="玄幻封面",
                status="selected",
                selection_state="selected",
                score=0.82,
                width=600,
                height=800,
                file_size_bytes=1024,
                file_path="/tmp/cover.png",
                mime_type="image/png",
            )
            session.add(cover)
            session.flush()
            work.cover_asset_id = cover.id
            work.cover_state = "generated"

        with Session() as session:
            stored_work = session.execute(select(PublisherWorkBinding)).scalar_one()
            stored_cover = session.execute(select(PublisherCoverAsset)).scalar_one()
            assert stored_work.cover_asset_id == stored_cover.id
            assert stored_cover.selection_state == "selected"
    finally:
        engine.dispose()


def test_upload_job_defaults_to_chapter_upload_task_kind() -> None:
    engine, Session = _session_factory("publisher-task-kind-default")
    try:
        with Session.begin() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                book_name="Book",
                chapter_title="Chapter",
                body_text="Body",
            )
            session.add(job)
        with Session() as session:
            stored = session.execute(select(PublisherUploadJob)).scalar_one()
            assert stored.task_kind == "chapter_upload"
    finally:
        engine.dispose()


def test_upload_success_upserts_work_binding_from_receipt() -> None:
    engine, runtime = _runtime("publisher-upload-upserts-work")
    try:
        with runtime.session_factory() as session:
            project_id = _project(session)
            session.commit()

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="qidian",
            book_name="绑定测试",
            chapter_title="第一章",
            body="正文不应写入 binding payload",
            upload_url=None,
            publish=True,
        )
        updated = _complete_upload(
            runtime,
            created,
            current_url="https://write.qq.com/portal/book/123",
            result_payload={
                "remote_book_id": "book-123",
                "remote_book_url": "https://write.qq.com/portal/book/123",
                "remote_chapter_id": "chapter-1",
                "chapter_number": 1,
                "official_status": "published",
                "audit_state": "under_review",
            },
        )

        assert updated["result_payload"]["work_binding"]["remote_book_id"] == "book-123"
        with runtime.session_factory() as session:
            work = session.execute(select(PublisherWorkBinding)).scalar_one()
            assert work.project_id == project_id
            assert work.platform_id == "qidian"
            assert work.book_name == "绑定测试"
            assert work.remote_book_id == "book-123"
            assert work.remote_url == ""
            assert work.audit_state == "unknown"
            assert "正文不应写入 binding payload" not in work.raw_payload_json
    finally:
        engine.dispose()


def test_upload_success_upserts_chapter_binding() -> None:
    engine, runtime = _runtime("publisher-upload-upserts-chapter")
    try:
        with runtime.session_factory() as session:
            project_id = _project(session)
            session.commit()

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="fanqie",
            book_name="章节绑定",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=True,
        )
        updated = _complete_upload(
            runtime,
            created,
            current_url="https://fanqienovel.com/main/writer/chapter-manage/456",
            result_payload={
                "work_id": "456",
                "remote_chapter_id": "chapter-456-1",
                "chapter_number": 1,
                "official_status": "published",
            },
        )

        assert (
            updated["result_payload"]["chapter_binding"]["remote_chapter_id"]
            == "chapter-456-1"
        )
        with runtime.session_factory() as session:
            chapter = session.execute(select(PublisherChapterBinding)).scalar_one()
            assert chapter.project_id == project_id
            assert chapter.platform_id == "fanqie"
            assert chapter.chapter_number == 1
            assert chapter.chapter_title == "第一章"
            assert chapter.remote_chapter_id == "chapter-456-1"
            assert chapter.publish_state == "published"
            assert chapter.word_count == len("正文")
    finally:
        engine.dispose()


def test_fanqie_chapter_manage_url_backfills_remote_book_id() -> None:
    engine, runtime = _runtime("publisher-fanqie-url-book-id")
    try:
        with runtime.session_factory() as session:
            project_id = _project(session)
            session.commit()

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="fanqie",
            book_name="番茄绑定",
            chapter_title="测试章",
            body="正文",
            upload_url=None,
            publish=False,
        )
        current_url = "https://fanqienovel.com/main/writer/chapter-manage/7624577204235537433&%E7%95%AA%E8%8C%84%E7%BB%91%E5%AE%9A?type=2"
        updated = _complete_upload(
            runtime,
            created,
            current_url=current_url,
            result_payload={
                "official_status": "drafted",
                "verified_via": "chapter-manage",
                "remote_chapter_id": "chapter-7624577204235537433-1",
            },
        )

        assert (
            updated["result_payload"]["work_binding"]["remote_book_id"]
            == "7624577204235537433"
        )
        assert (
            updated["result_payload"]["chapter_binding"]["remote_chapter_id"]
            == "chapter-7624577204235537433-1"
        )
        with runtime.session_factory() as session:
            work = session.execute(select(PublisherWorkBinding)).scalar_one()
            chapter = session.execute(select(PublisherChapterBinding)).scalar_one()
            assert work.remote_book_id == "7624577204235537433"
            canonical_url = current_url.split("?", 1)[0]
            assert work.remote_url == ""
            assert chapter.remote_url == canonical_url
            assert chapter.publish_state == "drafted"
    finally:
        engine.dispose()


def test_qidian_chaptertmp_url_backfills_book_and_chapter_ids() -> None:
    engine, runtime = _runtime("publisher-qidian-url-ids")
    try:
        with runtime.session_factory() as session:
            project_id = _project(session)
            session.commit()

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="qidian",
            book_name="起点绑定",
            chapter_title="测试章",
            body="正文",
            upload_url=None,
            publish=False,
        )
        current_url = (
            "https://write.qq.com/portal/booknovels/chaptertmp/CBID/35512915704247809"
            "?entry=publish#ccid=96252713670601666"
        )
        updated = _complete_upload(
            runtime,
            created,
            current_url=current_url,
            result_payload={
                "official_status": "drafted",
                "verified_via": "chapter-page",
            },
        )

        assert (
            updated["result_payload"]["work_binding"]["remote_book_id"]
            == "35512915704247809"
        )
        assert (
            updated["result_payload"]["chapter_binding"]["remote_chapter_id"]
            == "96252713670601666"
        )
        with runtime.session_factory() as session:
            work = session.execute(select(PublisherWorkBinding)).scalar_one()
            chapter = session.execute(select(PublisherChapterBinding)).scalar_one()
            assert work.remote_book_id == "35512915704247809"
            assert chapter.remote_chapter_id == "96252713670601666"
            assert chapter.remote_url == (
                "https://write.qq.com/portal/booknovels/chaptertmp/CBID/"
                "35512915704247809?ccid=96252713670601666"
            )
            assert chapter.publish_state == "drafted"
    finally:
        engine.dispose()


def test_upload_result_reuses_existing_work_binding_for_project_platform() -> None:
    engine, runtime = _runtime("publisher-upload-reuses-work")
    try:
        with runtime.session_factory() as session:
            project_id = _project(session)
            existing = PublisherWorkBinding(
                project_id=project_id,
                platform_id="qidian",
                book_name="旧名",
                remote_book_id="old-book",
            )
            session.add(existing)
            session.commit()
            existing_id = existing.id

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="qidian",
            book_name="新名",
            chapter_title="第二章",
            body="正文",
            upload_url=None,
            publish=True,
        )
        _complete_upload(
            runtime,
            created,
            current_url="https://write.qq.com/portal/book/999",
            result_payload={
                "remote_book_id": "new-book",
                "remote_chapter_id": "chapter-2",
                "chapter_number": 2,
                "official_status": "published",
            },
        )

        with runtime.session_factory() as session:
            works = session.execute(select(PublisherWorkBinding)).scalars().all()
            assert len(works) == 1
            assert works[0].id == existing_id
            assert works[0].remote_book_id == "new-book"
            assert works[0].book_name == "新名"
    finally:
        engine.dispose()
