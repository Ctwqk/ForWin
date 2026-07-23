from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from forwin.publisher_runtime.covers import PublisherCoverService
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


def _run_cover_generation_job(
    runtime: PublisherRuntimeService,
    *,
    project_id: str,
    platform_id: str,
    book_name: str,
    candidate_count: int,
) -> dict:
    with runtime.session_factory() as session:
        job = PublisherUploadJob(
            project_id=project_id,
            platform_id=platform_id,
            task_kind="cover_generate",
            status="pending",
            book_name=book_name,
            chapter_title="",
            body_text="",
            result_payload_json=json.dumps(
                {
                    "project_id": project_id,
                    "book_meta": _book_meta(),
                    "cover_candidate_count": candidate_count,
                },
                ensure_ascii=False,
            ),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    assert runtime.backend_jobs.run_pending_once(limit=1) == [job_id]
    with runtime.session_factory() as session:
        stored = session.get(PublisherUploadJob, job_id)
        assert stored is not None
        assert stored.status == "succeeded"
        return json.loads(stored.result_payload_json)


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


def test_default_cover_directory_uses_shared_data_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORWIN_PUBLISHER_COVER_DIR", raising=False)

    service = PublisherCoverService(
        session_factory=object(),
        image_client=FakeImageClient([]),
    )

    assert service.cover_dir == Path("data/publisher_covers")


def test_cover_generation_is_owned_by_backend_job_runner() -> None:
    assert not hasattr(PublisherCoverService, "generate_cover_candidates")


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

        result = _run_cover_generation_job(
            runtime,
            project_id=project_id,
            platform_id="qidian",
            book_name="封面测试",
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
        result = _run_cover_generation_job(
            runtime,
            project_id="project-score",
            platform_id="fanqie",
            book_name="高分封面",
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
        result = _run_cover_generation_job(
            runtime,
            project_id="project-invalid",
            platform_id="qidian",
            book_name="有效封面",
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


def test_cover_job_rolls_back_assets_when_terminal_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-job-atomic-terminal",
        rows=[{"bytes": PNG_1X1, "score": 0.5, "request_id": "req-atomic"}],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="原子封面",
                chapter_title="",
                body_text="",
                result_payload_json='{"project_id":"project-atomic","book_meta":{}}',
            )
            session.add(job)
            session.commit()
            job_id = job.id

        monkeypatch.setattr(
            runtime.cover_service,
            "enqueue_cover_upload_if_ready",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("terminal update fault")
            ),
        )

        runtime.backend_jobs.run_pending_once(limit=1)

        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            covers = session.execute(
                select(PublisherCoverAsset)
            ).scalars().all()
            assert stored is not None
            assert stored.status == "failed"
            assert covers == []
        assert list(tmp_path.rglob("*.png")) == []
        assert list(tmp_path.rglob("*.jpg")) == []
        assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    finally:
        engine.dispose()


def test_cover_job_preserves_committed_files_when_commit_ack_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-job-commit-ack-lost",
        rows=[{"bytes": PNG_1X1, "score": 0.5, "request_id": "req-ack-lost"}],
        cover_dir=tmp_path,
    )
    listener_raised = False
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="提交结果不确定",
                chapter_title="",
                body_text="",
                result_payload_json='{"project_id":"project-ack-lost","book_meta":{}}',
            )
            session.add(job)
            session.commit()
            job_id = job.id

        original_commit = runtime.session_factory.class_.commit

        def commit_then_lose_ack(session) -> None:
            nonlocal listener_raised
            original_commit(session)
            if not listener_raised and session.info.get(
                "publisher_cover_staged_files"
            ):
                listener_raised = True
                raise RuntimeError("commit acknowledgement lost")

        monkeypatch.setattr(
            runtime.session_factory.class_,
            "commit",
            commit_then_lose_ack,
        )
        runtime.backend_jobs.run_pending_once(limit=1)

        assert listener_raised is True
        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            covers = session.execute(select(PublisherCoverAsset)).scalars().all()
            assert stored is not None
            assert stored.status == "succeeded"
            assert len(covers) == 1
            assert Path(covers[0].file_path).is_file()
    finally:
        engine.dispose()


def test_cover_job_scavenges_file_when_commit_outcome_check_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-job-commit-check-unavailable",
        rows=[{"bytes": PNG_1X1, "score": 0.5, "request_id": "req-check-down"}],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="提交核验不可用",
                chapter_title="",
                body_text="",
                result_payload_json='{"project_id":"project-check-down","book_meta":{}}',
            )
            session.add(job)
            session.commit()
            job_id = job.id

        original_commit = runtime.session_factory.class_.commit

        def fail_before_cover_commit(session) -> None:
            if session.info.get("publisher_cover_staged_files"):
                raise RuntimeError("database connection lost before commit")
            original_commit(session)

        monkeypatch.setattr(
            runtime.session_factory.class_,
            "commit",
            fail_before_cover_commit,
        )
        monkeypatch.setattr(
            runtime.cover_service,
            "_cover_assets_are_absent",
            lambda _asset_ids: False,
        )

        runtime.backend_jobs.run_pending_once(limit=1)

        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            covers = session.execute(select(PublisherCoverAsset)).scalars().all()
            assert stored is not None
            assert stored.status == "failed"
            assert covers == []
        runtime.backend_jobs.recover_interrupted_cover_jobs(now=NOW)
        assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    finally:
        engine.dispose()


def test_cover_job_removes_partial_file_when_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-job-partial-write",
        rows=[{"bytes": PNG_1X1, "score": 0.5, "request_id": "req-partial"}],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="部分文件",
                chapter_title="",
                body_text="",
                result_payload_json='{"project_id":"project-partial","book_meta":{}}',
            )
            session.add(job)
            session.commit()
            job_id = job.id

        def write_part_then_fail(path: Path, data: bytes) -> int:
            with path.open("wb") as handle:
                handle.write(data[:4])
            raise OSError("partial file write")

        monkeypatch.setattr(Path, "write_bytes", write_part_then_fail)

        runtime.backend_jobs.run_pending_once(limit=1)

        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            covers = session.execute(select(PublisherCoverAsset)).scalars().all()
            assert stored is not None
            assert stored.status == "failed"
            assert covers == []
        assert list(tmp_path.rglob("*.png")) == []
        assert list(tmp_path.rglob("*.jpg")) == []
        assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    finally:
        engine.dispose()


def test_cover_job_honors_abort_requested_during_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-job-abort-during-provider",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="取消封面",
                chapter_title="",
                body_text="",
                result_payload_json='{"project_id":"project-abort","book_meta":{}}',
            )
            session.add(job)
            session.commit()
            job_id = job.id

        def abort_then_return(**_kwargs):
            with runtime.session_factory.begin() as session:
                stored = session.get(PublisherUploadJob, job_id)
                assert stored is not None
                stored.status = "terminating"
                stored.abort_requested = True
            return [
                {
                    "bytes": PNG_1X1,
                    "score": 0.5,
                    "request_id": "req-abort",
                }
            ]

        monkeypatch.setattr(
            runtime.cover_service.image_client,
            "generate_images",
            abort_then_return,
        )

        runtime.backend_jobs.run_pending_once(limit=1)

        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            covers = session.execute(
                select(PublisherCoverAsset)
            ).scalars().all()
            assert stored is not None
            assert stored.status == "cancelled"
            assert stored.abort_requested is True
            assert covers == []
    finally:
        engine.dispose()


def test_cover_job_provider_failure_after_abort_stays_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-job-provider-failure-after-abort",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="失败后取消",
                chapter_title="",
                body_text="",
                result_payload_json='{"project_id":"project-abort-failure"}',
            )
            session.add(job)
            session.commit()
            job_id = job.id

        def abort_then_fail(**_kwargs):
            with runtime.session_factory.begin() as session:
                stored = session.get(PublisherUploadJob, job_id)
                assert stored is not None
                stored.status = "terminating"
                stored.abort_requested = True
            raise RuntimeError("provider interrupted after abort")

        monkeypatch.setattr(
            runtime.cover_service.image_client,
            "generate_images",
            abort_then_fail,
        )

        runtime.backend_jobs.run_pending_once(limit=1)

        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            assert stored is not None
            assert stored.status == "cancelled"
            assert stored.abort_requested is True
            assert stored.finished_at is not None
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
        backend_claim = runtime.backend_jobs.claim_next_cover_generate_job()
        assert backend_claim is not None
        backend_job_id = backend_claim.job_id
        assert backend_job_id != created["job_id"]
        assert runtime.upload_jobs.get_upload_job(backend_job_id)["status"] == "running"
        with runtime.session_factory() as session:
            claimed = session.get(PublisherUploadJob, backend_job_id)
            assert claimed is not None
            original_claimed_at = claimed.claimed_at
            original_started_at = claimed.started_at

        assert runtime.attempts.recover_interrupted(now=NOW) == []
        assert (
            runtime.upload_jobs.get_upload_job(backend_job_id)["status"]
            == "running"
        )
        recovered = runtime.backend_jobs.recover_interrupted_cover_jobs(
            now=NOW
        )

        assert recovered == [backend_job_id]
        stored = runtime.upload_jobs.get_upload_job(backend_job_id)
        assert stored["status"] == "pending"
        assert stored["extension_client_id"] == ""
        with runtime.session_factory() as session:
            row = session.get(PublisherUploadJob, backend_job_id)
            assert row is not None
            assert row.claimed_at == original_claimed_at
            assert row.started_at == original_started_at
            assert row.finished_at is None
        assert runtime.backend_jobs.recover_interrupted_cover_jobs(now=NOW) == []
        reclaimed = runtime.backend_jobs.claim_next_cover_generate_job()
        assert reclaimed is not None
        assert reclaimed.job_id == backend_job_id
        assert reclaimed.owner_token != backend_claim.owner_token
    finally:
        engine.dispose()


def test_stale_cover_claim_cannot_complete_or_fail_reclaimed_job(
    tmp_path: Path,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-stale-claim-fence",
        rows=[{"bytes": PNG_1X1, "score": 0.5, "request_id": "req-stale"}],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="迟到 claim",
                chapter_title="",
                body_text="",
                result_payload_json='{"project_id":"project-stale","book_meta":{}}',
            )
            session.add(job)
            session.commit()
            job_id = job.id

        stale_claim = runtime.backend_jobs.claim_next_cover_generate_job()
        assert stale_claim is not None
        assert stale_claim.job_id == job_id
        assert runtime.backend_jobs.recover_interrupted_cover_jobs(now=NOW) == [job_id]
        current_claim = runtime.backend_jobs.claim_next_cover_generate_job()
        assert current_claim is not None
        assert current_claim.job_id == job_id
        assert current_claim.owner_token != stale_claim.owner_token

        stale_result = runtime.cover_service.generate_for_job(
            job_id,
            owner_token=stale_claim.owner_token,
        )
        runtime.backend_jobs.mark_failed(
            job_id,
            RuntimeError("late provider failure"),
            owner_token=stale_claim.owner_token,
        )

        assert stale_result == {"ok": False, "stale_claim": True}
        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            covers = session.execute(select(PublisherCoverAsset)).scalars().all()
            assert stored is not None
            assert stored.status == "running"
            assert stored.extension_client_id == current_claim.owner_token
            assert covers == []

        result = runtime.cover_service.generate_for_job(
            job_id,
            owner_token=current_claim.owner_token,
        )
        assert result["ok"] is True
        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            covers = session.execute(select(PublisherCoverAsset)).scalars().all()
            assert stored is not None
            assert stored.status == "succeeded"
            assert len(covers) == 1
    finally:
        engine.dispose()


def test_stale_cover_failure_does_not_run_global_file_scavenger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-stale-cleanup-fence",
        rows=[],
        cover_dir=tmp_path,
    )
    cleanup_calls: list[bool] = []
    try:
        with runtime.session_factory() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                book_name="迟到清扫",
                chapter_title="",
                body_text="",
            )
            session.add(job)
            session.commit()
            job_id = job.id

        stale_claim = runtime.backend_jobs.claim_next_cover_generate_job()
        assert stale_claim is not None
        assert runtime.backend_jobs.recover_interrupted_cover_jobs(now=NOW) == [job_id]
        current_claim = runtime.backend_jobs.claim_next_cover_generate_job()
        assert current_claim is not None
        monkeypatch.setattr(
            runtime.backend_jobs,
            "claim_next_cover_generate_job",
            lambda: stale_claim,
        )
        monkeypatch.setattr(
            runtime.cover_service,
            "generate_for_job",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("late stale failure")
            ),
        )
        monkeypatch.setattr(
            runtime.cover_service,
            "cleanup_orphaned_files",
            lambda: cleanup_calls.append(True),
        )

        runtime.backend_jobs.run_pending_once(limit=1)

        assert cleanup_calls == []
        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, job_id)
            assert stored is not None
            assert stored.status == "running"
            assert stored.extension_client_id == current_claim.owner_token
    finally:
        engine.dispose()


def test_cover_recovery_scavenges_orphans_and_preserves_committed_files(
    tmp_path: Path,
) -> None:
    engine, runtime = _runtime(
        "publisher-cover-orphan-recovery",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        referenced = tmp_path / "project" / "qidian" / "referenced.png"
        referenced.parent.mkdir(parents=True)
        referenced.write_bytes(PNG_1X1)
        orphan = tmp_path / "project" / "qidian" / "orphan.png"
        orphan.write_bytes(PNG_1X1)
        partial = tmp_path / ".staging" / "partial.part"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(PNG_1X1[:4])
        with runtime.session_factory() as session:
            session.add(
                PublisherCoverAsset(
                    project_id="project",
                    source="minimax",
                    status="selected",
                    selection_state="selected",
                    file_path=str(referenced),
                    mime_type="image/png",
                )
            )
            session.commit()

        assert runtime.backend_jobs.recover_interrupted_cover_jobs(now=NOW) == []

        assert referenced.is_file()
        assert not orphan.exists()
        assert not partial.exists()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("status", "abort_requested"),
    [
        ("running", True),
        ("terminating", False),
    ],
)
def test_cover_recovery_cancels_abort_or_terminating_without_touching_other_jobs(
    tmp_path: Path,
    status: str,
    abort_requested: bool,
) -> None:
    engine, runtime = _runtime(
        f"publisher-cover-recovery-{status}-{abort_requested}",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        with runtime.session_factory() as session:
            cover = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status=status,
                abort_requested=abort_requested,
                extension_client_id="backend",
                claimed_at=NOW - timedelta(minutes=2),
                started_at=NOW - timedelta(minutes=1),
                book_name="中断封面",
                chapter_title="",
                body_text="",
                result_payload_json='{"identity":"preserve"}',
            )
            browser_job = PublisherUploadJob(
                platform_id="qidian",
                task_kind="chapter_upload",
                status="running",
                extension_client_id="browser-client",
                book_name="浏览器任务",
                chapter_title="第一章",
                body_text="正文",
            )
            attempt_owned_cover = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="running",
                extension_client_id="backend",
                current_attempt_id="attempt-owned",
                book_name="已有 attempt",
                chapter_title="",
                body_text="",
            )
            deleted_cover = PublisherUploadJob(
                platform_id="qidian",
                task_kind="cover_generate",
                status="running",
                extension_client_id="backend",
                deleted_at=NOW - timedelta(minutes=3),
                book_name="已删除封面",
                chapter_title="",
                body_text="",
            )
            session.add_all(
                [
                    cover,
                    browser_job,
                    attempt_owned_cover,
                    deleted_cover,
                ]
            )
            session.commit()
            cover_id = cover.id
            browser_job_id = browser_job.id
            attempt_owned_id = attempt_owned_cover.id
            deleted_id = deleted_cover.id

        recovered = runtime.backend_jobs.recover_interrupted_cover_jobs(
            now=NOW
        )

        assert recovered == [cover_id]
        with runtime.session_factory() as session:
            stored = session.get(PublisherUploadJob, cover_id)
            assert stored is not None
            assert stored.status == "cancelled"
            assert stored.finished_at is not None
            assert stored.finished_at.replace(tzinfo=timezone.utc) == NOW
            assert stored.extension_client_id == ""
            assert stored.claimed_at == (
                NOW - timedelta(minutes=2)
            ).replace(tzinfo=None)
            assert stored.started_at == (
                NOW - timedelta(minutes=1)
            ).replace(tzinfo=None)
            assert stored.result_payload_json == '{"identity":"preserve"}'

            browser = session.get(PublisherUploadJob, browser_job_id)
            assert browser is not None
            assert browser.status == "running"
            assert browser.extension_client_id == "browser-client"

            attempt_owned = session.get(
                PublisherUploadJob,
                attempt_owned_id,
            )
            assert attempt_owned is not None
            assert attempt_owned.status == "running"
            assert attempt_owned.current_attempt_id == "attempt-owned"

            deleted = session.get(PublisherUploadJob, deleted_id)
            assert deleted is not None
            assert deleted.status == "running"
    finally:
        engine.dispose()


def test_publisher_backend_worker_lock_is_singleton(tmp_path: Path) -> None:
    engine, runtime = _runtime(
        "publisher-cover-singleton-worker",
        rows=[],
        cover_dir=tmp_path,
    )
    try:
        with runtime.backend_jobs.singleton_worker_lock():
            with engine.connect() as observer:
                advisory_states = observer.execute(
                    text(
                        """
                        SELECT activity.state
                        FROM pg_locks AS locks
                        JOIN pg_stat_activity AS activity
                          ON activity.pid=locks.pid
                        WHERE locks.locktype='advisory'
                          AND activity.datname=current_database()
                        """
                    )
                ).scalars().all()
            assert advisory_states
            assert "idle in transaction" not in advisory_states
            with pytest.raises(
                RuntimeError,
                match="already active",
            ):
                with runtime.backend_jobs.singleton_worker_lock():
                    raise AssertionError("second worker lock must not open")

        with runtime.backend_jobs.singleton_worker_lock():
            pass
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
        runtime.attempts.recover_interrupted(now=NOW + timedelta(seconds=12))

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
