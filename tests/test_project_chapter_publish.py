from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from forwin.api_schema import ProjectChapterPublishRequest
from forwin.application.projects.chapters import create_project_chapter_upload_job
from forwin.models.project import ChapterPlan
from forwin.models.publisher import PublisherUploadJob
from tests.test_canon_publisher_jobs import _fixture, _materialize


def test_manual_chapter_publish_releases_the_exact_canon_job() -> None:
    fixture = _fixture("project-chapter-publish")
    manager = SimpleNamespace(
        find_canon_job=fixture.runtime.canon_jobs.find,
        release_canon_jobs=fixture.runtime.canon_jobs.release,
    )
    request = ProjectChapterPublishRequest(
        platform="qidian",
        chapter_number=fixture.chapter_number,
        book_name="手动发布快照",
        upload_url="https://write.example/manual",
        publish=False,
        publisher_compliance_required=False,
    )
    try:
        event_jobs = _materialize(fixture, publish=False)
        first = create_project_chapter_upload_job(
            fixture.project_id,
            request,
            get_session=fixture.runtime.session_factory,
            publisher_manager=manager,
        )
        with fixture.runtime.session_factory.begin() as session:
            chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
            assert chapter is not None
            chapter.title = "第七章 后改标题"

        replay = create_project_chapter_upload_job(
            fixture.project_id,
            request.model_copy(update={"book_name": "后来修改的书名"}),
            get_session=fixture.runtime.session_factory,
            publisher_manager=manager,
        )

        assert first.job_id == replay.job_id
        assert first.status == "pending"
        assert first.publish is False
        assert first.job_id == event_jobs[0]["job_id"]
        assert replay.book_name == "事件时书名"
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadJob.id))) == 1
            job = session.execute(select(PublisherUploadJob)).scalar_one()
            assert job.canon_commit_id == fixture.canon_commit_id
            assert job.candidate_id == fixture.candidate_id
            assert job.chapter_number == fixture.chapter_number
            assert len(job.idempotency_key) == 64
    finally:
        fixture.engine.dispose()


def test_manual_chapter_publish_waits_for_canon_event_materialization() -> None:
    fixture = _fixture("project-chapter-publish-await-event")
    manager = SimpleNamespace(
        find_canon_job=fixture.runtime.canon_jobs.find,
        release_canon_jobs=fixture.runtime.canon_jobs.release,
    )
    request = ProjectChapterPublishRequest(
        platform="qidian",
        chapter_number=fixture.chapter_number,
        book_name="不能抢先写入的手动快照",
        publish=True,
        publisher_compliance_required=False,
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            create_project_chapter_upload_job(
                fixture.project_id,
                request,
                get_session=fixture.runtime.session_factory,
                publisher_manager=manager,
            )

        assert exc_info.value.status_code == 409
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadJob.id))) == 0
    finally:
        fixture.engine.dispose()
