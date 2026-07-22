from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from forwin.publisher_runtime.service import PublisherRuntimeService
from tests.postgres import postgres_test_url


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


class FakeImageClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def generate_images(self, *, prompt: str, model: str = "image-01", count: int = 4):
        self.calls.append({"prompt": prompt, "model": model, "count": count})
        return list(self.rows)[:count]


def _runtime(
    name: str, *, rows, cover_dir: Path
) -> tuple[object, PublisherRuntimeService]:
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, PublisherRuntimeService(
        session_factory=get_session_factory(engine),
        extension_api_key="secret",
        heartbeat_stale_seconds=90,
        preferred_client_id="",
        publisher_session_secret="",
        publisher_session_encryption_required=False,
        minimax_image_client=FakeImageClient(rows),
        publisher_cover_dir=str(cover_dir),
    )


def _project(session, title: str = "封面测试") -> str:
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


def _book_meta() -> dict:
    return {
        "audience": "male",
        "primary_category": "玄幻",
        "intro": "这是一本用于测试封面生成链路的玄幻小说，包含主角、冲突、长线悬念和适合商业网文封面的视觉提示。",
        "protagonist_names": ["韩砚"],
    }


def _complete_upload(runtime: PublisherRuntimeService, created: dict, payload: dict):
    claimed = runtime.upload_jobs.claim_next_upload_job(
        client_id="client-1",
        connected_platforms=[created["platform"]],
    )
    assert claimed is not None
    assert claimed["job_id"] == created["job_id"]
    current_url = "https://write.qq.com/portal/book/123"
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
            "remote_book_id": str(payload.get("remote_book_id") or "book-123"),
            "remote_chapter_id": str(payload.get("remote_chapter_id") or "chapter-1"),
            "remote_url": current_url,
            "official_state": str(payload.get("official_status") or "published"),
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


def test_cover_generation_stores_multiple_candidates(tmp_path: Path) -> None:
    engine, runtime = _runtime(
        "publisher-cover-store-candidates",
        rows=[
            {"bytes": PNG_1X1, "score": 0.2, "request_id": "req-1"},
            {"bytes": PNG_1X1, "score": 0.5, "request_id": "req-1"},
            {"bytes": PNG_1X1, "score": 0.1, "request_id": "req-1"},
        ],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            project_id = _project(session)
            session.commit()

        result = runtime.cover_service.generate_cover_candidates(
            project_id=project_id,
            platform_id="qidian",
            book_name="封面测试",
            book_meta=_book_meta(),
            candidate_count=3,
        )

        assert result["ok"] is True
        assert len(result["cover_asset_ids"]) == 3
        with runtime.session_factory() as session:
            covers = session.execute(select(PublisherCoverAsset)).scalars().all()
            assert len(covers) == 3
            assert (
                sum(1 for cover in covers if cover.selection_state == "selected") == 1
            )
            assert all(Path(cover.file_path).is_file() for cover in covers)
    finally:
        engine.dispose()


def test_cover_generation_prefers_highest_valid_score(tmp_path: Path) -> None:
    engine, runtime = _runtime(
        "publisher-cover-high-score",
        rows=[
            {"bytes": PNG_1X1, "score": 0.1, "request_id": "req-1"},
            {"bytes": PNG_1X1, "score": 0.9, "request_id": "req-1"},
        ],
        cover_dir=tmp_path,
    )
    try:
        result = runtime.cover_service.generate_cover_candidates(
            project_id="project-score",
            platform_id="fanqie",
            book_name="高分封面",
            book_meta=_book_meta(),
            candidate_count=2,
        )

        with runtime.session_factory() as session:
            selected = session.get(
                PublisherCoverAsset, result["selected_cover_asset_id"]
            )
            assert selected is not None
            assert selected.score == 0.9
    finally:
        engine.dispose()


def test_invalid_cover_is_not_selected_when_valid_candidate_exists(
    tmp_path: Path,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-invalid-skipped",
        rows=[
            {"bytes": b"not-image", "score": 1.0, "request_id": "req-1"},
            {"bytes": PNG_1X1, "score": 0.1, "request_id": "req-1"},
        ],
        cover_dir=tmp_path,
    )
    try:
        result = runtime.cover_service.generate_cover_candidates(
            project_id="project-invalid",
            platform_id="qidian",
            book_name="有效封面",
            book_meta=_book_meta(),
            candidate_count=2,
        )

        with runtime.session_factory() as session:
            selected = session.get(
                PublisherCoverAsset, result["selected_cover_asset_id"]
            )
            assert selected is not None
            assert selected.file_size_bytes == len(PNG_1X1)
    finally:
        engine.dispose()


def test_cover_generation_job_marks_failed_when_no_valid_candidates(
    tmp_path: Path,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-job-failed",
        rows=[{"bytes": b"not-image", "score": 1.0, "request_id": "req-1"}],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="失败封面",
                chapter_title="",
                body_text="",
                result_payload_json='{"project_id":"project-failed","book_meta":{}}',
            )
            session.add(job)
            session.commit()
            job_id = job.id

        runtime.backend_jobs.run_pending_once(limit=1)

        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            assert stored is not None
            assert stored.status == "failed"
    finally:
        engine.dispose()


def test_upload_creation_enqueues_cover_generate_for_create_if_missing(
    tmp_path: Path,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-create-enqueue",
        rows=[{"bytes": PNG_1X1, "score": 0.5, "request_id": "req-1"}],
        cover_dir=tmp_path,
    )
    try:
        created = runtime.upload_jobs.create_upload_job(
            platform="qidian",
            book_name="自动封面",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=True,
            create_if_missing=True,
            book_meta=_book_meta(),
        )

        with runtime.session_factory() as session:
            kinds = (
                session.execute(select(PublisherUploadJob.task_kind)).scalars().all()
            )
            assert created["task_kind"] == "chapter_upload"
            assert "cover_generate" in kinds
            assert "chapter_upload" in kinds
    finally:
        engine.dispose()


def test_restart_returns_backend_cover_generation_to_its_owned_queue(
    tmp_path: Path,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-generate-restart-recovery",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        created = runtime.upload_jobs.create_upload_job(
            platform="qidian",
            book_name="重启封面",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=True,
            create_if_missing=True,
            book_meta=_book_meta(),
        )
        backend_job_id = runtime.backend_jobs.claim_next_cover_generate_job()
        assert backend_job_id
        assert backend_job_id != created["job_id"]
        assert runtime.upload_jobs.get_upload_job(backend_job_id)["status"] == "running"

        recovered = runtime.attempts.recover_interrupted(now=NOW)

        assert backend_job_id in recovered
        stored = runtime.upload_jobs.get_upload_job(backend_job_id)
        assert stored["status"] == "pending"
        assert stored["extension_client_id"] == ""
        assert runtime.backend_jobs.claim_next_cover_generate_job() == backend_job_id
    finally:
        engine.dispose()


def test_selected_cover_is_enqueued_for_upload_after_first_chapter_success(
    tmp_path: Path,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-upload-after-chapter",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        cover_file = tmp_path / "cover.png"
        cover_file.write_bytes(PNG_1X1)
        with runtime.session_factory() as session:
            project_id = _project(session)
            session.add(
                PublisherCoverAsset(
                    project_id=project_id,
                    source="minimax",
                    status="selected",
                    selection_state="selected",
                    width=1,
                    height=1,
                    file_size_bytes=len(PNG_1X1),
                    file_path=str(cover_file),
                    mime_type="image/png",
                )
            )
            session.commit()

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="qidian",
            book_name="封面上传",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=True,
        )
        updated = _complete_upload(
            runtime,
            created,
            {
                "remote_book_id": "book-123",
                "remote_chapter_id": "chapter-1",
                "chapter_number": 1,
                "official_status": "published",
            },
        )

        assert updated["result_payload"]["cover_upload_job_id"]
        with runtime.session_factory() as session:
            cover_uploads = (
                session.execute(
                    select(PublisherUploadJob).where(
                        PublisherUploadJob.task_kind == "cover_upload"
                    )
                )
                .scalars()
                .all()
            )
            assert len(cover_uploads) == 1
            assert "cover_asset_id" in cover_uploads[0].result_payload_json
    finally:
        engine.dispose()


def test_cover_upload_identity_deduplicates_every_job_state(tmp_path: Path) -> None:
    engine, runtime = _runtime(
        "publisher-cover-idempotent-all-states",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        cover_file = tmp_path / "cover.png"
        cover_file.write_bytes(PNG_1X1)
        with runtime.session_factory() as session:
            project_id = _project(session)
            work = PublisherWorkBinding(
                project_id=project_id,
                platform_id="qidian",
                book_name="封面幂等",
                remote_book_id="book-cover",
            )
            session.add(work)
            session.flush()
            cover = PublisherCoverAsset(
                project_id=project_id,
                work_binding_id=work.id,
                status="selected",
                selection_state="selected",
                file_path=str(cover_file),
                mime_type="image/png",
            )
            session.add(cover)
            session.commit()
            cover_id = cover.id

        first = runtime.cover_service.enqueue_cover_upload(cover_id)
        assert len(first.idempotency_key) == 64
        for status in ("reconciling", "terminating", "paused", "cancelled"):
            with runtime.session_factory() as session:
                stored = session.get(PublisherUploadJob, first.id)
                assert stored is not None
                stored.status = status
                session.commit()
            replay = runtime.cover_service.enqueue_cover_upload(cover_id)
            assert replay.id == first.id
            assert replay.idempotency_key == first.idempotency_key

        with runtime.session_factory() as session:
            cover_jobs = (
                session.execute(
                    select(PublisherUploadJob).where(
                        PublisherUploadJob.task_kind == "cover_upload"
                    )
                )
                .scalars()
                .all()
            )
            assert len(cover_jobs) == 1
    finally:
        engine.dispose()


def test_late_cover_receipt_converges_job_binding_and_asset(tmp_path: Path) -> None:
    engine, runtime = _runtime(
        "publisher-cover-late-receipt-convergence",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        cover_file = tmp_path / "cover.png"
        cover_file.write_bytes(PNG_1X1)
        with runtime.session_factory() as session:
            project_id = _project(session)
            work = PublisherWorkBinding(
                project_id=project_id,
                platform_id="qidian",
                book_name="迟到封面",
                remote_book_id="book-cover",
            )
            session.add(work)
            session.flush()
            cover = PublisherCoverAsset(
                project_id=project_id,
                work_binding_id=work.id,
                status="selected",
                selection_state="selected",
                file_path=str(cover_file),
                mime_type="image/png",
            )
            session.add(cover)
            session.commit()
            work_id = work.id
            cover_id = cover.id

        job = runtime.cover_service.enqueue_cover_upload(cover_id)
        claim = runtime.attempts.claim(
            client_id="extension-late-cover",
            connected_platforms=["qidian"],
            lease_seconds=10,
            now=NOW,
        )
        assert claim is not None
        runtime.attempts.transition(
            job_id=job.id,
            attempt_id=claim["attempt_id"],
            worker_id="extension-late-cover",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            lease_seconds=10,
            now=NOW + timedelta(seconds=1),
        )
        runtime.attempts.expire(now=NOW + timedelta(seconds=12))

        applied = runtime.upload_jobs.record_upload_receipt(
            job_id=job.id,
            attempt_id=claim["attempt_id"],
            client_id="extension-late-cover",
            lease_epoch=claim["lease_epoch"],
            receipt={
                "remote_book_id": "book-cover",
                "remote_chapter_id": "",
                "remote_url": "https://write.qq.com/portal/book/cover",
                "official_state": "cover_uploaded",
                "content_sha256": job.body_sha256,
                "evidence": {
                    "content_sha256": job.body_sha256,
                    "confirmation_text": "accepted",
                },
            },
            now=NOW + timedelta(seconds=13),
        )

        assert applied["status"] == "succeeded"
        assert applied["receipt_disposition"] == "late_applied"
        with runtime.session_factory() as session:
            stored_work = session.get(PublisherWorkBinding, work_id)
            stored_cover = session.get(PublisherCoverAsset, cover_id)
            assert stored_work is not None
            assert stored_cover is not None
            assert stored_work.cover_state == "uploaded"
            assert stored_cover.status == "uploaded"
    finally:
        engine.dispose()


def test_cover_generation_completion_enqueues_cover_upload_when_first_chapter_already_succeeded(
    tmp_path: Path,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-complete-after-chapter",
        rows=[{"bytes": PNG_1X1, "score": 0.7, "request_id": "req-1"}],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            project_id = _project(session)
            session.commit()

        created = runtime.upload_jobs.create_upload_job(
            project_id=project_id,
            platform="qidian",
            book_name="补传封面",
            chapter_title="第一章",
            body="正文",
            upload_url=None,
            publish=True,
            create_if_missing=True,
            book_meta=_book_meta(),
        )
        _complete_upload(
            runtime,
            created,
            {
                "remote_book_id": "book-123",
                "remote_chapter_id": "chapter-1",
                "chapter_number": 1,
                "official_status": "published",
            },
        )

        runtime.backend_jobs.run_pending_once(limit=1)

        with runtime.session_factory() as session:
            cover_uploads = (
                session.execute(
                    select(PublisherUploadJob).where(
                        PublisherUploadJob.task_kind == "cover_upload"
                    )
                )
                .scalars()
                .all()
            )
            assert len(cover_uploads) == 1
    finally:
        engine.dispose()
