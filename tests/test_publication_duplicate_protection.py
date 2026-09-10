from __future__ import annotations

import pytest
from sqlalchemy import select

from forwin.models.canon import CanonPublicationProtection
from forwin.models.publisher import PublisherUploadJob
from forwin.publisher_runtime.protection import reserve_publication
from tests.test_canon_publisher_jobs import _fixture, _materialize


@pytest.mark.parametrize("state", ["reserved", "published"])
def test_other_jobs_protection_blocks_new_mutation_even_after_original_job_deleted(
    state,
):
    fixture = _fixture("duplicate-publication-" + state)
    try:
        jobs = _materialize(fixture)
        with fixture.runtime.session_factory.begin() as session:
            job = session.get(PublisherUploadJob, jobs[0]["job_id"])
            job.publish = False
            session.add(
                CanonPublicationProtection(
                    project_id=fixture.project_id,
                    chapter_plan_id=fixture.chapter_plan_id,
                    chapter_number=fixture.chapter_number,
                    canon_commit_id=fixture.canon_commit_id,
                    upload_job_id="deleted-original-job",
                    platform_id=job.platform_id,
                    content_sha256=job.body_sha256,
                    state=state,
                )
            )
        with fixture.runtime.session_factory.begin() as session:
            job = session.get(PublisherUploadJob, jobs[0]["job_id"])
            with pytest.raises(
                ValueError, match="existing protection.*deleted-original-job"
            ) as raised:
                reserve_publication(session, job)
            assert raised.value.conflicting_job_id == "deleted-original-job"
            assert raised.value.reason == "publication_conflict"
            assert len(list(session.scalars(select(CanonPublicationProtection)))) == 1
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize(
    "state,platform", [("confirmed_absent", "qidian"), ("reserved", "fanqie")]
)
def test_absent_or_other_platform_protection_does_not_block_this_platform(
    state, platform
):
    fixture = _fixture("nonconflicting-publication-" + state)
    try:
        jobs = _materialize(fixture)
        with fixture.runtime.session_factory.begin() as session:
            job = session.get(PublisherUploadJob, jobs[0]["job_id"])
            job.publish = False
            session.add(
                CanonPublicationProtection(
                    project_id=fixture.project_id,
                    chapter_plan_id=fixture.chapter_plan_id,
                    chapter_number=fixture.chapter_number,
                    canon_commit_id=fixture.canon_commit_id,
                    upload_job_id="different-job",
                    platform_id=platform,
                    content_sha256=job.body_sha256,
                    state=state,
                )
            )
            session.flush()
            reserve_publication(session, job)
            assert (
                session.scalar(
                    select(CanonPublicationProtection.state).where(
                        CanonPublicationProtection.upload_job_id == job.id
                    )
                )
                == "reserved"
            )
    finally:
        fixture.engine.dispose()
