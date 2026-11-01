from __future__ import annotations

import base64
import json

import pytest

from forwin.models.base import new_id
from forwin.models.publisher import (
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from forwin.publish_hub.refs import (
    chapter_key,
    cover_key,
    cover_source_ref_for,
    create_key,
    source_ref_for,
)
from forwin.publisher_runtime.idempotency import (
    publisher_cover_upload_idempotency_key,
    publisher_job_idempotency_key,
)
from tests.test_canon_publisher_jobs import _fixture, _materialize

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _chapter_job(
    *,
    candidate_id: str = "cand-1",
    platform_id: str = "fanqie",
    canon_key: str = "canon-key-1",
    chapter_number: int = 3,
) -> PublisherUploadJob:
    return PublisherUploadJob(
        id="job-1",
        project_id="p1",
        candidate_id=candidate_id,
        chapter_number=chapter_number,
        platform_id=platform_id,
        task_kind="chapter_upload",
        book_name="雾港",
        chapter_title="第三章",
        body_text="正文",
        publish=True,
        result_payload_json=json.dumps(
            {"canon_identity": {"canon_idempotency_key": canon_key}}
        ),
    )


def test_source_ref_names_the_project_and_chapter() -> None:
    assert source_ref_for("p1", 3) == "project/p1/chapter/3"
    assert source_ref_for(" p1 ", "12") == "project/p1/chapter/12"


@pytest.mark.parametrize(
    ("project_id", "chapter_number"),
    [("", 1), ("  ", 1), ("p1", 0), ("p1", -2), ("p1", "x")],
)
def test_source_ref_rejects_an_incomplete_identity(project_id, chapter_number) -> None:
    with pytest.raises(ValueError):
        source_ref_for(project_id, chapter_number)


def test_a_regenerated_candidate_keeps_the_source_ref_but_not_the_key() -> None:
    first = _chapter_job(candidate_id="cand-1")
    regenerated = _chapter_job(candidate_id="cand-2")

    assert source_ref_for(first.project_id, first.chapter_number) == source_ref_for(
        regenerated.project_id, regenerated.chapter_number
    )
    assert "cand" not in source_ref_for(first.project_id, first.chapter_number)
    assert chapter_key(first) != chapter_key(regenerated)


def test_chapter_key_is_the_existing_identity_with_the_forwin_prefix() -> None:
    job = _chapter_job()

    expected = publisher_job_idempotency_key(
        canon_idempotency_key="canon-key-1",
        project_id="p1",
        chapter_number=3,
        candidate_id="cand-1",
        platform_id="fanqie",
    )

    assert chapter_key(job) == "forwin:" + expected
    assert chapter_key(job) != chapter_key(_chapter_job(platform_id="qidian"))
    assert chapter_key(job) != chapter_key(_chapter_job(chapter_number=4))


def test_chapter_key_needs_a_canon_identity() -> None:
    job = _chapter_job()
    job.result_payload_json = "{}"

    with pytest.raises(ValueError, match="canon_idempotency_key"):
        chapter_key(job)


def test_chapter_key_equals_the_prefixed_stored_key_of_a_canon_materialised_job() -> None:
    fixture = _fixture("publish-hub-chapter-key")
    try:
        [created] = _materialize(fixture)
        with fixture.runtime.session_factory() as session:
            job = session.get(PublisherUploadJob, created["job_id"])

            assert job.idempotency_key
            assert chapter_key(job) == "forwin:" + job.idempotency_key
    finally:
        fixture.engine.dispose()


def test_cover_key_equals_the_prefixed_stored_key_of_a_cover_upload_job(tmp_path) -> None:
    fixture = _fixture("publish-hub-cover-key")
    try:
        cover_file = tmp_path / "cover.png"
        cover_file.write_bytes(PNG_1X1)
        with fixture.runtime.session_factory() as session:
            work = PublisherWorkBinding(
                project_id=fixture.project_id,
                platform_id="qidian",
                book_name="封面书",
                remote_book_id="book-1",
            )
            session.add(work)
            session.flush()
            cover = PublisherCoverAsset(
                project_id=fixture.project_id,
                work_binding_id=work.id,
                status="selected",
                selection_state="selected",
                file_path=str(cover_file),
                mime_type="image/png",
            )
            session.add(cover)
            session.commit()
            cover_id = cover.id

        created = fixture.runtime.cover_service.enqueue_cover_upload(cover_id)

        with fixture.runtime.session_factory() as session:
            job = session.get(PublisherUploadJob, created.id)
            assert job.task_kind == "cover_upload" and len(job.idempotency_key) == 64
            assert cover_key(job) == "forwin:" + job.idempotency_key
    finally:
        fixture.engine.dispose()


def _cover_job(*, cover_asset_id: str = "asset-1", sha: str = "a" * 64) -> PublisherUploadJob:
    return PublisherUploadJob(
        id=new_id(),
        project_id="p1",
        platform_id="qidian",
        task_kind="cover_upload",
        body_sha256=sha,
        result_payload_json=json.dumps(
            {
                "project_id": "p1",
                "work_binding_id": "work-1",
                "cover_asset_id": cover_asset_id,
                "file_path": "/app/data/publisher_covers/p1/qidian/asset.png",
            }
        ),
    )


def test_cover_key_is_computed_from_the_job_fields() -> None:
    job = _cover_job()

    expected = publisher_cover_upload_idempotency_key(
        work_binding_id="work-1",
        platform_id="qidian",
        cover_asset_id="asset-1",
        content_sha256="a" * 64,
    )

    assert cover_key(job) == "forwin:" + expected
    assert cover_key(job) != cover_key(_cover_job(cover_asset_id="asset-2"))
    assert cover_key(job) != cover_key(_cover_job(sha="b" * 64))


def test_cover_key_rejects_a_job_that_is_not_a_cover_upload() -> None:
    with pytest.raises(ValueError, match="cover_upload"):
        cover_key(_chapter_job())


def test_cover_source_ref_is_one_publication_per_cover_asset() -> None:
    assert cover_source_ref_for("p1", "asset-1") == "project/p1/cover/asset-1"
    assert cover_source_ref_for("p1", "asset-1") == cover_source_ref_for("p1", "asset-1")
    assert cover_source_ref_for("p1", "asset-1") != cover_source_ref_for("p1", "asset-2")
    assert cover_source_ref_for("p1", "asset-1") != cover_source_ref_for("p2", "asset-1")
    with pytest.raises(ValueError):
        cover_source_ref_for("p1", "")
    with pytest.raises(ValueError):
        cover_source_ref_for("", "asset-1")


def test_create_key_has_the_documented_shape() -> None:
    assert create_key("p1", "fanqie", "abc123") == "forwin:create:p1:fanqie:abc123"
    assert create_key("p1", "fanqie", "abc123") != create_key("p1", "qidian", "abc123")
    assert create_key("p1", "fanqie", "abc123") != create_key("p2", "fanqie", "abc123")
    assert create_key("p1", "fanqie", "abc123") != create_key("p1", "fanqie", "def456")
    with pytest.raises(ValueError):
        create_key("p1", "fanqie", "")


def test_every_key_carries_the_forwin_prefix() -> None:
    assert chapter_key(_chapter_job()).startswith("forwin:")
    assert cover_key(_cover_job()).startswith("forwin:")
    assert create_key("p1", "fanqie", "abc").startswith("forwin:")
