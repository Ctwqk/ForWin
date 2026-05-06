from __future__ import annotations

import json

from sqlalchemy import select

from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.governance import DecisionEvent
from forwin.models.project import Project
from forwin.publisher_runtime.service import PublisherRuntimeService
from forwin.publishers.manager import PublisherManager


def _runtime(name: str) -> tuple[object, PublisherRuntimeService]:
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, PublisherRuntimeService(
        session_factory=get_session_factory(engine),
        extension_api_key="secret",
        heartbeat_stale_seconds=90,
        preferred_client_id="",
        publisher_session_secret="",
        publisher_session_encryption_required=False,
    )


def test_upload_job_service_lifecycle_preserves_payload_and_audit_shape() -> None:
    engine, runtime = _runtime("publisher-runtime-upload")
    project_id = new_id()
    try:
        with runtime.session_factory() as session:
            session.add(
                Project(
                    id=project_id,
                    title="发布运行时",
                    premise="测试 premise",
                    genre="玄幻",
                    setting_summary="",
                )
            )
            session.commit()

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="qidian",
            book_name="发布运行时",
            chapter_title="第一章",
            body="正文不应写入审计 payload",
            upload_url=None,
            publish=False,
            create_if_missing=True,
            book_meta={"primary_category": "都市日常", "protagonist_names": ["沈砚", "林雾", "多余"]},
        )
        claimed = runtime.upload_jobs.claim_next_upload_job(
            client_id="client-1",
            connected_platforms=["qidian"],
        )
        updated = runtime.upload_jobs.update_upload_job_result(
            job_id=created["job_id"],
            client_id="client-1",
            status="succeeded",
            message="完成",
            current_url="https://write.qq.com/portal/dashboard",
            error="",
            result_payload={"remote_chapter_id": "remote-1"},
        )

        assert claimed is not None
        assert claimed["job_id"] == created["job_id"]
        assert updated["status"] == "succeeded"
        assert updated["result_payload"]["create_if_missing"] is True
        assert updated["result_payload"]["book_meta"]["protagonist_names"] == ["沈砚", "林雾"]
        assert runtime.upload_jobs.get_upload_job(created["job_id"])["deletable"] is True

        with runtime.session_factory() as session:
            events = session.execute(
                select(DecisionEvent)
                .where(DecisionEvent.project_id == project_id)
                .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
            ).scalars().all()

        event_types = [event.event_type for event in events]
        assert DecisionEventType.UPLOAD_JOB_CREATED in event_types
        assert DecisionEventType.UPLOAD_JOB_CLAIMED in event_types
        assert DecisionEventType.UPLOAD_JOB_SUCCEEDED in event_types
        for event in events:
            payload_text = event.payload_json or "{}"
            assert "正文不应写入审计 payload" not in payload_text
            payload = json.loads(payload_text)
            if event.event_type.startswith("upload_job_"):
                assert payload["body_chars"] == len("正文不应写入审计 payload")
    finally:
        engine.dispose()


def test_upload_job_service_available_through_manager_runtime_facade() -> None:
    engine, runtime = _runtime("publisher-runtime-manager-upload")
    manager = PublisherManager(
        runtime.session_factory,
        extension_api_key="secret",
    )
    try:
        created = manager.runtime.upload_jobs.create_upload_job(
            platform="fanqie",
            book_name="测试书",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=False,
        )
        facade = manager.get_upload_job(created["job_id"])

        assert facade["job_id"] == created["job_id"]
        assert facade["message"] == "番茄小说 上传任务已创建，等待浏览器扩展执行。"
    finally:
        engine.dispose()
