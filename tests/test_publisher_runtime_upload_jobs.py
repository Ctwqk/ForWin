from __future__ import annotations

import json
import hashlib

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.audit import DecisionEvent
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherConnectionState,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from forwin.publisher_runtime.service import PublisherRuntimeService
from forwin.publishers.manager import PublisherManager
from tests.postgres import postgres_test_url


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


def _complete_mutating_job(
    runtime: PublisherRuntimeService,
    created: dict,
    claimed: dict,
    *,
    current_url: str,
    remote_book_id: str = "book-1",
    remote_chapter_id: str = "chapter-1",
    official_state: str = "drafted",
    details: dict | None = None,
    message: str = "完成",
) -> dict:
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
        attempt_id=claimed["attempt_id"],
        client_id="client-1",
        lease_epoch=claimed["lease_epoch"],
        receipt={
            "remote_book_id": remote_book_id,
            "remote_chapter_id": remote_chapter_id,
            "remote_url": current_url,
            "official_state": official_state,
            "content_sha256": created["body_sha256"],
            "evidence": {
                "verified": True,
                "content_sha256": created["body_sha256"],
                "confirmation_text": "accepted",
            },
        },
    )
    return runtime.upload_jobs.update_upload_job_result(
        job_id=created["job_id"],
        client_id="client-1",
        attempt_id=claimed["attempt_id"],
        lease_epoch=claimed["lease_epoch"],
        outcome="succeeded",
        message=message,
        current_url=current_url,
        error_code="",
        error_message="",
        details=details or {},
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
            book_meta={
                "audience": "male",
                "primary_category": "都市日常",
                "protagonist_names": ["韩砚", "林雾", "多余"],
                "intro": "这是一本关于发布运行时的长篇测试作品，简介用于通过平台建书预检，并验证 payload 不泄露正文。",
            },
        )
        claimed = runtime.upload_jobs.claim_next_upload_job(
            client_id="client-1",
            connected_platforms=["qidian"],
        )
        assert claimed is not None
        assert claimed["idempotency_key"].startswith("publisher-job:v1:")
        assert len(claimed["body_sha256"]) == 64
        updated = _complete_mutating_job(
            runtime,
            created,
            claimed,
            current_url="https://write.qq.com/portal/dashboard",
            remote_chapter_id="remote-1",
        )

        assert claimed["job_id"] == created["job_id"]
        assert updated["status"] == "succeeded"
        assert updated["result_payload"]["create_if_missing"] is True
        assert updated["result_payload"]["book_meta"]["protagonist_names"] == [
            "韩砚",
            "林雾",
        ]
        assert (
            updated["result_payload"]["platform_meta"]["resolved_primary_category"][
                "label"
            ]
            == "都市"
        )
        assert updated["result_payload"]["preflight"]["ok"] is True
        assert (
            runtime.upload_jobs.get_upload_job(created["job_id"])["deletable"] is True
        )

        with runtime.session_factory() as session:
            events = (
                session.execute(
                    select(DecisionEvent)
                    .where(DecisionEvent.project_id == project_id)
                    .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
                )
                .scalars()
                .all()
            )

        event_types = [event.event_type for event in events]
        assert DecisionEventType.UPLOAD_JOB_CREATED in event_types
        assert DecisionEventType.UPLOAD_JOB_CLAIMED in event_types
        assert DecisionEventType.UPLOAD_JOB_SUCCEEDED in event_types
        for event in events:
            payload_text = event.payload_json or "{}"
            assert "正文不应写入审计 payload" not in payload_text
            payload = json.loads(payload_text)
            if event.event_type.startswith("upload_job_"):
                if payload.get("task_kind") == "cover_generate":
                    assert payload["body_chars"] == 0
                else:
                    assert payload["body_chars"] == len("正文不应写入审计 payload")
    finally:
        engine.dispose()


def test_create_if_missing_upload_job_blocks_hard_preflight_failure() -> None:
    engine, runtime = _runtime("publisher-runtime-preflight-block")
    try:
        try:
            runtime.upload_jobs.create_upload_job(
                platform="fanqie",
                book_name="测试书",
                chapter_title="第一章",
                body="正文",
                upload_url=None,
                publish=True,
                create_if_missing=True,
                book_meta={
                    "audience": "male",
                    "primary_category": "都市日常",
                    "intro": "太短",
                },
            )
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected preflight failure")

        assert "发布预检失败" in message
        assert "主角名" in message or "简介" in message
    finally:
        engine.dispose()


def test_upload_job_runs_required_publisher_compliance_review() -> None:
    engine, runtime = _runtime("publisher-runtime-compliance-review-pass")
    try:
        created = runtime.upload_jobs.create_upload_job(
            platform="qidian",
            book_name="测试书",
            chapter_title="第一章",
            body="夜雨落在街角，主角沿着灯影走进旧楼。",
            upload_url=None,
            publish=True,
            publisher_compliance_required=True,
        )

        assert created["status"] == "pending"
        preflight = created["result_payload"]["preflight"]
        assert preflight["ok"] is True
        assert preflight["publisher_compliance"]["verdict"] == "pass"
    finally:
        engine.dispose()


def test_upload_job_blocks_failed_publisher_compliance_review() -> None:
    engine, runtime = _runtime("publisher-runtime-compliance-review-fail")
    try:
        try:
            runtime.upload_jobs.create_upload_job(
                platform="qidian",
                book_name="测试书",
                chapter_title="第一章",
                body="主角说：加微信 vx123456 领取番外。",
                upload_url=None,
                publish=True,
                publisher_compliance_required=True,
            )
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected publisher compliance failure")

        assert "平台合规 reviewer" in message
    finally:
        engine.dispose()


def test_claim_next_upload_job_does_not_return_cover_generate() -> None:
    engine, runtime = _runtime("publisher-runtime-claim-skips-cover-generate")
    try:
        with runtime.session_factory() as session:
            session.add(
                PublisherUploadJob(
                    platform_id="qidian",
                    task_kind="cover_generate",
                    status="pending",
                    book_name="测试书",
                    chapter_title="",
                    body_text="",
                )
            )
            session.commit()

        claimed = runtime.upload_jobs.claim_next_upload_job(
            client_id="client-1",
            connected_platforms=["qidian"],
        )

        assert claimed is None
    finally:
        engine.dispose()


def test_terminal_upload_success_rejects_late_failure_result() -> None:
    engine, runtime = _runtime("publisher-runtime-terminal-result-ignored")
    try:
        created = runtime.upload_jobs.create_upload_job(
            platform="qidian",
            book_name="测试书",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=False,
        )
        claimed = runtime.upload_jobs.claim_next_upload_job(
            client_id="client-1",
            connected_platforms=["qidian"],
        )
        assert claimed is not None

        succeeded = _complete_mutating_job(
            runtime,
            created,
            claimed,
            current_url="https://write.qq.com/portal/booknovels/chaptertmp/CBID/123456?entry=publish#ccid=987654321",
            remote_book_id="123456",
            remote_chapter_id="987654321",
            message="章节草稿已保存到起点。",
        )
        with pytest.raises(ValueError, match="fence is no longer current"):
            runtime.upload_jobs.update_upload_job_result(
                job_id=created["job_id"],
                client_id="client-1",
                attempt_id=claimed["attempt_id"],
                lease_epoch=claimed["lease_epoch"],
                outcome="failed",
                message="上传失败。",
                current_url="https://write.qq.com/portal/login",
                error_code="platform-not-ready",
                error_message="平台页面没有准备好，无法执行上传。",
                details={},
            )

        assert succeeded["status"] == "succeeded"
        stored = runtime.upload_jobs.get_upload_job(created["job_id"])
        assert stored["status"] == "succeeded"
        assert stored["message"] == "章节草稿已保存到起点。"
        assert stored["error"] == ""
        assert stored["current_url"] == (
            "https://write.qq.com/portal/booknovels/chaptertmp/CBID/123456"
            "?ccid=987654321"
        )
        assert stored["result_payload"]["receipt"]["remote_chapter_id"] == ("987654321")

        with runtime.session_factory() as session:
            state = session.get(PublisherConnectionState, "qidian")
            assert state is not None
            assert state.connected is True
            assert state.last_error == ""
    finally:
        engine.dispose()


def test_pre_mutation_login_error_code_marks_platform_disconnected() -> None:
    engine, runtime = _runtime("publisher-runtime-login-error-code")
    try:
        created = runtime.upload_jobs.create_upload_job(
            platform="qidian",
            book_name="测试书",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=False,
        )
        claimed = runtime.upload_jobs.claim_next_upload_job(
            client_id="client-1",
            connected_platforms=["qidian"],
        )
        assert claimed is not None

        result = runtime.upload_jobs.update_upload_job_result(
            job_id=created["job_id"],
            client_id="client-1",
            attempt_id=claimed["attempt_id"],
            lease_epoch=claimed["lease_epoch"],
            outcome="failed",
            message="platform request rejected",
            current_url="",
            error_code="login-required",
            error_message="platform request rejected",
            details={},
        )

        assert result["status"] == "pending"
        with runtime.session_factory() as session:
            state = session.get(PublisherConnectionState, "qidian")
            assert state is not None
            assert state.connected is False
            assert state.last_error == "platform request rejected"
    finally:
        engine.dispose()


def test_claim_next_upload_job_returns_cover_upload_and_audit_sync() -> None:
    engine, runtime = _runtime("publisher-runtime-claim-task-kinds")
    try:
        with runtime.session_factory() as session:
            jobs = []
            for task_kind, payload in (
                (
                    "cover_upload",
                    {
                        "work_binding_id": "work-1",
                        "remote_book_id": "book-1",
                        "cover_asset_id": "cover-1",
                        "file_path": "/tmp/cover.png",
                    },
                ),
                (
                    "audit_sync",
                    {
                        "work_binding_id": "work-1",
                        "remote_book_id": "book-1",
                    },
                ),
            ):
                job_id = new_id()
                jobs.append(
                    PublisherUploadJob(
                        id=job_id,
                        idempotency_key=f"publisher-job:v1:{job_id}",
                        body_sha256=hashlib.sha256(
                            f"{task_kind}:{job_id}".encode()
                        ).hexdigest(),
                        platform_id="qidian",
                        task_kind=task_kind,
                        status="pending",
                        book_name="测试书",
                        chapter_title="",
                        body_text="",
                        result_payload_json=json.dumps(payload),
                    ),
                )
            session.add_all(jobs)
            session.commit()

        first = runtime.upload_jobs.claim_next_upload_job(
            client_id="client-1",
            connected_platforms=["qidian"],
        )
        assert first is not None
        if first["task_kind"] == "cover_upload":
            _complete_mutating_job(
                runtime,
                first,
                first,
                current_url="",
                remote_book_id="book-1",
                remote_chapter_id="",
                official_state="cover_uploaded",
                details={"cover_state": "uploaded"},
            )
        else:
            runtime.attempts.transition(
                job_id=first["job_id"],
                attempt_id=first["attempt_id"],
                worker_id="client-1",
                lease_epoch=first["lease_epoch"],
                phase="observation_started",
            )
            with pytest.raises(ValueError, match="identity does not match"):
                runtime.upload_jobs.update_upload_job_result(
                    job_id=first["job_id"],
                    client_id="client-1",
                    attempt_id=first["attempt_id"],
                    lease_epoch=first["lease_epoch"],
                    outcome="succeeded",
                    message="错误审核身份",
                    current_url="",
                    error_code="",
                    error_message="",
                    details={
                        "work": {
                            "work_binding_id": "work-other",
                            "remote_book_id": "book-1",
                            "audit_state": "unknown",
                        },
                        "chapters": [],
                        "cover": {"cover_state": "unknown"},
                        "milestones": [],
                    },
                )
            runtime.upload_jobs.update_upload_job_result(
                job_id=first["job_id"],
                client_id="client-1",
                attempt_id=first["attempt_id"],
                lease_epoch=first["lease_epoch"],
                outcome="succeeded",
                message="审核同步完成",
                current_url="",
                error_code="",
                error_message="",
                details={
                    "work": {
                        "work_binding_id": "work-1",
                        "remote_book_id": "book-1",
                        "audit_state": "unknown",
                    },
                    "chapters": [],
                    "cover": {"cover_state": "unknown"},
                    "milestones": [],
                },
            )
        second = runtime.upload_jobs.claim_next_upload_job(
            client_id="client-1",
            connected_platforms=["qidian"],
        )

        assert second is not None
        assert {first["task_kind"], second["task_kind"]} == {
            "cover_upload",
            "audit_sync",
        }
    finally:
        engine.dispose()


def test_manual_create_upload_job_returns_chapter_upload_task_kind() -> None:
    engine, runtime = _runtime("publisher-runtime-manual-task-kind")
    try:
        created = runtime.upload_jobs.create_upload_job(
            platform="fanqie",
            book_name="测试书",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=False,
        )

        assert created["task_kind"] == "chapter_upload"
    finally:
        engine.dispose()


def test_audit_sync_uses_exact_work_binding_identity() -> None:
    engine, runtime = _runtime("publisher-runtime-audit-binding-identity")
    manager = PublisherManager(runtime.session_factory, extension_api_key="secret")
    try:
        with runtime.session_factory() as session:
            work = PublisherWorkBinding(
                project_id="project-bound",
                platform_id="qidian",
                book_name="绑定作品",
                remote_book_id="book-bound",
                remote_url="https://write.qq.com/book/bookmanage/bookdetail/100/200",
            )
            session.add(work)
            session.commit()
            work_binding_id = work.id

        with pytest.raises(ValueError, match="项目与作品绑定不一致"):
            manager.enqueue_audit_sync(
                project_id="project-other",
                platform="qidian",
                work_binding_id=work_binding_id,
            )
        with pytest.raises(ValueError, match="平台与作品绑定不一致"):
            manager.enqueue_audit_sync(
                project_id="project-bound",
                platform="fanqie",
                work_binding_id=work_binding_id,
            )
        with pytest.raises(ValueError, match="书名与作品绑定不一致"):
            manager.enqueue_audit_sync(
                project_id="project-bound",
                platform="qidian",
                work_binding_id=work_binding_id,
                book_name="另一部作品",
            )

        created = manager.enqueue_audit_sync(
            project_id="project-bound",
            platform="qidian",
            work_binding_id=work_binding_id,
            book_name="绑定作品",
        )

        assert created["project_id"] == "project-bound"
        assert created["platform"] == "qidian"
        assert created["book_name"] == "绑定作品"
        assert created["result_payload"]["work_binding_id"] == work_binding_id
        assert created["result_payload"]["remote_book_id"] == "book-bound"
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
