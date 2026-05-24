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


def test_upload_success_upserts_work_binding_from_remote_payload() -> None:
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
        updated = runtime.upload_jobs.update_upload_job_result(
            job_id=created["job_id"],
            client_id="client-1",
            status="succeeded",
            message="完成",
            current_url="https://write.qq.com/portal/book/123",
            error="",
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
            assert work.remote_url == "https://write.qq.com/portal/book/123"
            assert work.audit_state == "under_review"
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
        updated = runtime.upload_jobs.update_upload_job_result(
            job_id=created["job_id"],
            client_id="client-1",
            status="succeeded",
            message="完成",
            current_url="https://fanqienovel.com/main/writer/chapter-manage/456",
            error="",
            result_payload={
                "work_id": "book-456",
                "remote_chapter_id": "chapter-456-1",
                "chapter_number": 1,
                "official_status": "published",
            },
        )

        assert updated["result_payload"]["chapter_binding"]["remote_chapter_id"] == "chapter-456-1"
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
        updated = runtime.upload_jobs.update_upload_job_result(
            job_id=created["job_id"],
            client_id="client-1",
            status="succeeded",
            message="完成",
            current_url=current_url,
            error="",
            result_payload={
                "official_status": "drafted",
                "verified_via": "chapter-manage",
            },
        )

        assert updated["result_payload"]["work_binding"]["remote_book_id"] == "7624577204235537433"
        assert updated["result_payload"]["chapter_binding"]["remote_chapter_id"] == ""
        with runtime.session_factory() as session:
            work = session.execute(select(PublisherWorkBinding)).scalar_one()
            chapter = session.execute(select(PublisherChapterBinding)).scalar_one()
            assert work.remote_book_id == "7624577204235537433"
            assert work.remote_url == current_url
            assert chapter.remote_url == current_url
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
        updated = runtime.upload_jobs.update_upload_job_result(
            job_id=created["job_id"],
            client_id="client-1",
            status="succeeded",
            message="完成",
            current_url=current_url,
            error="",
            result_payload={
                "official_status": "drafted",
                "verified_via": "chapter-page",
            },
        )

        assert updated["result_payload"]["work_binding"]["remote_book_id"] == "35512915704247809"
        assert updated["result_payload"]["chapter_binding"]["remote_chapter_id"] == "96252713670601666"
        with runtime.session_factory() as session:
            work = session.execute(select(PublisherWorkBinding)).scalar_one()
            chapter = session.execute(select(PublisherChapterBinding)).scalar_one()
            assert work.remote_book_id == "35512915704247809"
            assert chapter.remote_chapter_id == "96252713670601666"
            assert chapter.remote_url == current_url
            assert chapter.publish_state == "drafted"
    finally:
        engine.dispose()


def test_verified_draft_upserts_chapter_binding_as_drafted() -> None:
    engine, runtime = _runtime("publisher-upload-upserts-draft")
    try:
        with runtime.session_factory() as session:
            project_id = _project(session)
            session.commit()

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="qidian",
            book_name="草稿绑定",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=False,
        )
        runtime.upload_jobs.update_upload_job_result(
            job_id=created["job_id"],
            client_id="client-1",
            status="failed",
            message="上传失败。",
            current_url=(
                "https://write.qq.com/portal/booknovels/chaptertmp/CBID/123"
                "?entry=publish#ccid=96252587187165183"
            ),
            error="浏览器扩展执行超时，未能完成平台章节流程。",
            result_payload={
                "remote_book_id": "book-789",
                "error_code": "extension-upload-timeout",
            },
        )

        with runtime.session_factory() as session:
            chapter = session.execute(select(PublisherChapterBinding)).scalar_one()
            assert chapter.publish_state == "drafted"
            assert chapter.audit_state == "unknown"
            assert "qidian-real-ccid-timeout-recovery" in chapter.raw_payload_json
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
        runtime.upload_jobs.update_upload_job_result(
            job_id=created["job_id"],
            client_id="client-1",
            status="succeeded",
            message="完成",
            current_url="https://write.qq.com/portal/book/999",
            error="",
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
