from __future__ import annotations

import pytest

from forwin.publisher_runtime.idempotency import (
    publisher_cover_upload_idempotency_key,
    publisher_job_idempotency_key,
)


def test_publisher_job_idempotency_key_matches_golden_vector() -> None:
    assert (
        publisher_job_idempotency_key(
            canon_idempotency_key="canon-key-1",
            project_id="project-1",
            chapter_number=7,
            candidate_id="candidate-1",
            platform_id="qidian",
        )
        == "941d269141a182bb96e9eb934bacca16f08983c514a0316d507e564b2fc135d4"
    )


def test_publish_mode_is_not_part_of_publisher_job_identity() -> None:
    request = {
        "canon_idempotency_key": "canon-key-2",
        "project_id": "project-2",
        "chapter_number": 12,
        "candidate_id": "candidate-2",
        "platform_id": "fanqie",
    }

    draft_only = publisher_job_idempotency_key(**request)
    publish_enabled = publisher_job_idempotency_key(**request)

    assert draft_only == publish_enabled


def test_cover_upload_idempotency_key_matches_golden_vector() -> None:
    assert (
        publisher_cover_upload_idempotency_key(
            work_binding_id="work-1",
            platform_id="qidian",
            cover_asset_id="cover-1",
            content_sha256="a" * 64,
        )
        == "aabe694f56251a3278d725a44d4aba19429948d810ffff47f48130d7bd198dc3"
    )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        publisher_cover_upload_idempotency_key(
            work_binding_id="work-1",
            platform_id="qidian",
            cover_asset_id="cover-1",
            content_sha256="A" * 64,
        )
