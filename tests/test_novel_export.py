"""A local read-only projection of retained accepted chapter identities."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService
from forwin.models.draft import ChapterDraft
from forwin.models.outbox import OutboxEvent
from forwin.models.project import Project
from forwin.novel_export.events import NOVEL_EXPORT_REQUESTED, export_event
from forwin.novel_export.exporter import write_export
from forwin.novel_export.snapshot import capture_snapshot, render_snapshot
from forwin.outbox.store import claim_next_outbox_event
from tests import test_canon_atomic_transaction as atomic

prepared_canon = atomic.prepared_canon


def _accepted(fixture):
    result = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert not result.blocked, result
    with fixture.Session.begin() as session:
        return capture_snapshot(
            session,
            project_id=fixture.project_id,
            book_revision=1,
            display_title="Frozen title",
        )


def test_canon_enqueues_export_without_doing_filesystem_io(prepared_canon, monkeypatch):
    from forwin.novel_export import events

    def forbidden_io(*args):
        pytest.fail("Canon must not write export files")

    monkeypatch.setattr(events, "write_export", forbidden_io)
    snapshot = _accepted(prepared_canon)
    with prepared_canon.Session() as session:
        row = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == NOVEL_EXPORT_REQUESTED)
        )
        assert row is not None
        assert json.loads(row.payload_json)["book_revision"] == 1
        assert (
            snapshot.chapters[0].canon_commit_id == prepared_canon.plan.canon_commit_id
        )
        assert "Shen Linchuan" in render_snapshot(session, snapshot)


def test_frozen_export_retries_and_older_events_cannot_move_current_back(
    prepared_canon, tmp_path
):
    snapshot = _accepted(prepared_canon)
    with prepared_canon.Session() as session:
        book = render_snapshot(session, snapshot)
    write_export(tmp_path, snapshot, book)
    old_manifest = (
        tmp_path / snapshot.project_id / "revisions/1/manifest.json"
    ).read_bytes()
    newer = snapshot.model_copy(update={"book_revision": 2, "base_book_revision": 1})
    write_export(tmp_path, newer, book)
    write_export(tmp_path, snapshot, book)
    current = json.loads((tmp_path / snapshot.project_id / "current.json").read_text())
    assert current["book_revision"] == 2
    assert (
        tmp_path / snapshot.project_id / "revisions/1/manifest.json"
    ).read_bytes() == old_manifest


def test_partial_write_never_publishes_current_and_retry_completes(
    prepared_canon, tmp_path, monkeypatch
):
    from forwin.novel_export import exporter

    snapshot = _accepted(prepared_canon)
    with prepared_canon.Session() as session:
        book = render_snapshot(session, snapshot)
    original = exporter.write_managed_text_if_changed

    def fail_manifest(root, path, content, **kwargs):
        if path.endswith("manifest.json"):
            raise OSError("full disk")
        return original(root, path, content, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(exporter, "write_managed_text_if_changed", fail_manifest)
        with pytest.raises(OSError, match="full disk"):
            write_export(tmp_path, snapshot, book)
    assert not (tmp_path / snapshot.project_id / "current.json").exists()
    write_export(tmp_path, snapshot, book)
    assert (
        json.loads((tmp_path / snapshot.project_id / "current.json").read_text())[
            "book_revision"
        ]
        == 1
    )


def test_frozen_body_identity_refuses_changed_draft_without_exporting(prepared_canon):
    snapshot = _accepted(prepared_canon)
    with prepared_canon.Session.begin() as session:
        session.get(
            ChapterDraft, snapshot.chapters[0].draft_id
        ).body_text = "unproven replacement"
    with (
        prepared_canon.Session() as session,
        pytest.raises(ValueError, match="body identity"),
    ):
        render_snapshot(session, snapshot)


def test_capture_checks_current_active_identity_and_future_revision(prepared_canon):
    _accepted(prepared_canon)
    with (
        prepared_canon.Session() as session,
        pytest.raises(ValueError, match="revision"),
    ):
        capture_snapshot(
            session,
            project_id=prepared_canon.project_id,
            book_revision=2,
            display_title="Invalid future",
        )


def test_handler_freezes_publication_observation_before_io_and_rebuilds(
    prepared_canon, tmp_path, monkeypatch
):
    from forwin.novel_export import events

    _accepted(prepared_canon)
    with prepared_canon.Session() as session:
        row = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == NOVEL_EXPORT_REQUESTED)
        )
        row_id = row.id
    with prepared_canon.Session.begin() as session:
        # Other Canon observer events have their own handlers and are outside
        # this bounded worker test.
        for row in session.scalars(select(OutboxEvent).where(OutboxEvent.id != row_id)):
            row.status = "processed"
        session.flush()
        claim = claim_next_outbox_event(
            session, worker_id="export-test", lease_seconds=60
        )
    with monkeypatch.context() as patch:
        patch.setattr(
            events, "write_export", lambda *args: (_ for _ in ()).throw(OSError("disk"))
        )
        with pytest.raises(OSError):
            export_event(prepared_canon.Session, tmp_path, claim)
    with prepared_canon.Session.begin() as session:
        payload = json.loads(session.get(OutboxEvent, row_id).payload_json)
        assert payload["snapshot"]["chapters"]
        assert payload["snapshot"]["chapters"][0]["published_receipts"] == []
        session.get(Project, prepared_canon.project_id).title = "Changed later"
    export_event(prepared_canon.Session, tmp_path, claim)
    manifest = json.loads(
        (tmp_path / prepared_canon.project_id / "revisions/1/manifest.json").read_text()
    )
    assert manifest == payload["snapshot"]


def test_real_full_suffix_replacement_and_retained_world_edit_rebuild_each_book_revision(
    prepared_canon,
):
    from forwin.canon.revision_service import (
        RevisionValidationService,
        revision_model_identity,
        save_revision_proposal,
    )
    from forwin.runtime.policy import RuntimePolicy
    from forwin.writer import ChapterWriter
    from tests.test_revision_full_suffix import BodyModel, _book

    ids, body, old_ids, _ = _book(prepared_canon)
    writer = ChapterWriter(BodyModel())
    owner = RevisionValidationService(
        session_factory=prepared_canon.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    )
    with prepared_canon.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = proposal.id
    prepared = owner.prepare(project_id=ids[0], candidate_id=candidate_id)
    assert not prepared.blocked, prepared
    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        prepared.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert not outcome.blocked, outcome
    with prepared_canon.Session() as session:
        assert session.get(Project, ids[0]).book_revision == 3
        assert sorted(
            json.loads(row.payload_json)["book_revision"]
            for row in session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == ids[0],
                    OutboxEvent.event_type == NOVEL_EXPORT_REQUESTED,
                )
            )
        ) == [1, 2, 3]

    # Preserve a pre-boundary world edit and its original export request.
    # The current API rejects world edits on books with accepted chapters.
    atomic._seed_retained_legacy_world_edit(
        SimpleNamespace(Session=prepared_canon.Session, project_id=ids[0])
    )
    from forwin.canon.projection_lock import lock_projection_project
    from forwin.novel_export.events import enqueue_book_export

    with prepared_canon.Session.begin() as session:
        lock_projection_project(session, ids[0])
        enqueue_book_export(session, session.get(Project, ids[0]))
    with prepared_canon.Session.begin() as session:
        versions = [
            capture_snapshot(
                session,
                project_id=ids[0],
                book_revision=number,
                display_title="Frozen archive",
            )
            for number in range(1, 5)
        ]
        assert [len(snapshot.chapters) for snapshot in versions] == [1, 2, 2, 2]
        assert [chapter.canon_commit_id for chapter in versions[1].chapters] == old_ids
        assert all(
            chapter.canon_commit_id not in old_ids for chapter in versions[2].chapters
        )
        assert {chapter.base_book_revision for chapter in versions[2].chapters} == {2}
        assert versions[2].chapters == versions[3].chapters
        assert "安静地" in render_snapshot(session, versions[1])
        assert "静静地" in render_snapshot(session, versions[2])
        requests = list(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == ids[0],
                    OutboxEvent.event_type == NOVEL_EXPORT_REQUESTED,
                )
            )
        )
        assert sorted(
            json.loads(row.payload_json)["book_revision"] for row in requests
        ) == [1, 2, 3, 4]


def test_pre_acceptance_world_edit_api_enqueues_current_export_request(prepared_canon):
    from forwin.api_schema import WorldEditProposalReviewRequest
    from forwin.http.adapters.api_proposal_routes import build_handlers
    from tests.test_world_edit_history_boundary import _proposal

    proposal_id = _proposal(prepared_canon, 0)
    result = build_handlers(get_session=prepared_canon.Session)[
        "approve_project_proposal"
    ](
        prepared_canon.project_id,
        proposal_id,
        WorldEditProposalReviewRequest(status="accepted", reason="Writing premise"),
    )
    assert result.status == "accepted"
    with prepared_canon.Session() as session:
        project = session.get(Project, prepared_canon.project_id)
        assert project.book_revision == 1
        requests = list(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == project.id,
                    OutboxEvent.event_type == NOVEL_EXPORT_REQUESTED,
                )
            )
        )
        assert len(requests) == 1
        assert requests[0].event_id == f"novel-export:{project.id}:1"
        assert requests[0].status == "pending"
        payload = json.loads(requests[0].payload_json)
        assert payload["project_id"] == project.id
        assert payload["book_revision"] == 1
        assert payload["display_title"] == project.title
        assert payload["snapshot"] is None


def test_stale_claim_cannot_capture_or_overwrite_the_new_claim_snapshot(
    prepared_canon, tmp_path
):
    _accepted(prepared_canon)
    with prepared_canon.Session.begin() as session:
        for row in session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type != NOVEL_EXPORT_REQUESTED)
        ):
            row.status = "processed"
        session.flush()
        claim = claim_next_outbox_event(session, worker_id="current", lease_seconds=60)
    old = replace(claim, worker_id="old", lease_epoch=claim.lease_epoch - 1)
    with pytest.raises(ValueError, match="lease is stale"):
        export_event(prepared_canon.Session, tmp_path, old)
    with prepared_canon.Session() as session:
        assert (
            json.loads(session.get(OutboxEvent, claim.row_id).payload_json)["snapshot"]
            is None
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda _: export_event(prepared_canon.Session, tmp_path, claim),
                range(2),
            )
        )
    with prepared_canon.Session() as session:
        frozen = session.get(OutboxEvent, claim.row_id).payload_json
    with pytest.raises(ValueError, match="lease is stale"):
        export_event(prepared_canon.Session, tmp_path, old)
    with prepared_canon.Session() as session:
        assert session.get(OutboxEvent, claim.row_id).payload_json == frozen


def test_concurrent_revision_files_keep_newest_pointer_and_exact_body(
    prepared_canon, tmp_path
):
    from forwin.novel_export.snapshot import digest

    snapshot = _accepted(prepared_canon)
    with prepared_canon.Session() as session:
        book = render_snapshot(session, snapshot)
    newer_book = book + "New chapter body\n"
    newer = snapshot.model_copy(
        update={
            "book_revision": 2,
            "base_book_revision": 1,
            "book_sha256": digest(newer_book),
        }
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda pair: write_export(tmp_path, *pair),
                [
                    (snapshot, book),
                    (newer, newer_book),
                    (snapshot, book),
                    (newer, newer_book),
                ],
            )
        )
    current = json.loads((tmp_path / snapshot.project_id / "current.json").read_text())
    assert current["book_revision"] == 2
    assert (tmp_path / snapshot.project_id / current["book"]).read_text() == newer_book
    assert (tmp_path / snapshot.project_id / "revisions/1/book.md").read_text() == book


@pytest.mark.parametrize("location", ["project", "book", "lock"])
def test_symlink_cannot_redirect_book_or_lock_writes(
    prepared_canon, tmp_path, location
):
    snapshot = _accepted(prepared_canon)
    with prepared_canon.Session() as session:
        book = render_snapshot(session, snapshot)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("private sentinel")
    root = tmp_path / "exports"
    root.mkdir()
    if location == "project":
        (root / snapshot.project_id).symlink_to(outside, target_is_directory=True)
    elif location == "lock":
        (root / f".{snapshot.project_id}.lock").symlink_to(sentinel)
    else:
        folder = root / snapshot.project_id / "revisions/1"
        folder.mkdir(parents=True)
        (folder / "book.md").symlink_to(sentinel)
    with pytest.raises((OSError, ValueError)):
        write_export(root, snapshot, book)
    assert sentinel.read_text() == "private sentinel"
    assert sorted(path.name for path in outside.iterdir()) == ["keep"]


def test_outbox_io_failure_retries_without_changing_canon(
    prepared_canon, tmp_path, monkeypatch
):
    from forwin.novel_export import events
    from forwin.outbox.worker import run_one_outbox_event

    _accepted(prepared_canon)
    with prepared_canon.Session.begin() as session:
        for row in session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type != NOVEL_EXPORT_REQUESTED)
        ):
            row.status = "processed"
    handlers = events.build_novel_export_handlers(
        session_factory=prepared_canon.Session, root=tmp_path
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            events,
            "write_export",
            lambda *args: (_ for _ in ()).throw(OSError("disk full")),
        )
        first = run_one_outbox_event(
            session_factory=prepared_canon.Session,
            worker_id="failure",
            handlers=handlers,
            base_delay_seconds=0.01,
        )
    assert first.claimed and not first.processed
    with prepared_canon.Session.begin() as session:
        row = session.get(OutboxEvent, first.row_id)
        assert row.status == "pending" and row.attempts == 1
        assert json.loads(row.payload_json)["snapshot"]
        assert session.get(Project, prepared_canon.project_id).book_revision == 1
        row.available_at = None
    second = run_one_outbox_event(
        session_factory=prepared_canon.Session, worker_id="retry", handlers=handlers
    )
    assert second.processed


def test_manifest_retains_exact_published_references_without_exporting_operational_payloads(
    prepared_canon,
):
    from forwin.models.canon import CanonPublicationProtection
    from forwin.models.publisher import (
        PublisherUploadAttempt,
        PublisherUploadJob,
        PublisherUploadReceipt,
    )

    snapshot = _accepted(prepared_canon)
    chapter = snapshot.chapters[0]
    with prepared_canon.Session.begin() as session:
        body = session.get(ChapterDraft, chapter.draft_id).body_text
        job = PublisherUploadJob(
            project_id=snapshot.project_id,
            canon_commit_id=chapter.canon_commit_id,
            candidate_id=chapter.candidate_id,
            chapter_number=1,
            chapter_title=chapter.chapter_title,
            body_text=body,
            body_sha256=chapter.body_sha256,
            platform_id="test-platform",
        )
        session.add(job)
        session.flush()
        attempt = PublisherUploadAttempt(
            upload_job_id=job.id, attempt_number=1, attempt_kind="upload"
        )
        session.add(attempt)
        session.flush()
        receipt = PublisherUploadReceipt(
            upload_job_id=job.id,
            upload_attempt_id=attempt.id,
            receipt_key="test-receipt",
            platform_id=job.platform_id,
            remote_book_id="remote-book",
            remote_chapter_id="remote-chapter",
            official_state="published",
            content_sha256=chapter.body_sha256,
            evidence_json=json.dumps({"private_cookie": "MUST_NOT_EXPORT"}),
        )
        protection = CanonPublicationProtection(
            project_id=snapshot.project_id,
            chapter_plan_id=chapter.chapter_plan_id,
            chapter_number=1,
            canon_commit_id=chapter.canon_commit_id,
            upload_job_id=job.id,
            platform_id=job.platform_id,
            content_sha256=chapter.body_sha256,
            state="published",
            remote_book_id="remote-book",
            remote_chapter_id="remote-chapter",
            evidence_json=json.dumps({"reader_name": "MUST_NOT_EXPORT"}),
        )
        session.add_all([receipt, protection])
        session.flush()
        after = capture_snapshot(
            session,
            project_id=snapshot.project_id,
            book_revision=1,
            display_title="Book",
        )
        assert after.publication_reference_semantics == "observed_at_capture"
        ref = after.chapters[0].published_receipts[0]
        assert ref.publication_id == protection.id
        assert ref.receipt_ids == (receipt.id,)
        assert ref.receipt_status == "retained"
        assert "MUST_NOT_EXPORT" not in after.model_dump_json()
        assert snapshot.chapters[0].published_receipts == ()


def test_missing_frozen_plan_is_unknown_without_reading_current_plan(prepared_canon):
    from forwin.models.draft import CandidateDraftRecord
    from forwin.models.project import ChapterPlan

    initial = _accepted(prepared_canon)
    with prepared_canon.Session.begin() as session:
        session.get(
            CandidateDraftRecord, prepared_canon.candidate_id
        ).canon_commit_plan_json = "{}"
        session.get(
            ChapterPlan, prepared_canon.chapter_plan_id
        ).title = "Later mutable plan"
        snapshot = capture_snapshot(
            session,
            project_id=initial.project_id,
            book_revision=1,
            display_title="Book",
        )
        assert snapshot.chapters[0].plan_revision == initial.chapters[0].plan_revision
        assert snapshot.chapters[0].plan_revision_semantics == "candidate_original_plan"
        assert snapshot.chapters[0].frozen_plan.status == "unknown"
        assert snapshot.chapters[0].chapter_title == initial.chapters[0].chapter_title


def test_successful_export_can_rebuild_deleted_files_without_rewriting_outbox_state(
    prepared_canon, tmp_path
):
    import shutil

    from forwin.novel_export.events import build_novel_export_handlers, rebuild_export
    from forwin.outbox.worker import run_one_outbox_event

    snapshot = _accepted(prepared_canon)
    with prepared_canon.Session.begin() as session:
        for row in session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type != NOVEL_EXPORT_REQUESTED)
        ):
            row.status = "processed"
    result = run_one_outbox_event(
        session_factory=prepared_canon.Session,
        worker_id="export",
        handlers=build_novel_export_handlers(
            session_factory=prepared_canon.Session, root=tmp_path
        ),
    )
    assert result.processed
    folder = tmp_path / snapshot.project_id
    expected = {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }
    shutil.rmtree(folder)
    rebuild_export(
        prepared_canon.Session,
        tmp_path,
        project_id=snapshot.project_id,
        book_revision=1,
    )
    assert {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    } == expected
    with prepared_canon.Session() as session:
        event = session.get(OutboxEvent, result.row_id)
        assert event.status == "processed" and event.attempts == 1


def test_configured_root_cannot_escape_through_symlink_parent(prepared_canon, tmp_path):
    snapshot = _accepted(prepared_canon)
    with prepared_canon.Session() as session:
        book = render_snapshot(session, snapshot)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-parent"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_export(link / "novel_exports", snapshot, book)
    assert list(outside.iterdir()) == []


def test_existing_accepted_plan_hash_contradiction_cannot_be_downgraded_to_unknown(
    prepared_canon,
):
    from forwin.models.draft import CandidateDraftRecord
    from forwin.novel_export.snapshot import digest

    frozen = _accepted(prepared_canon)
    chapter = frozen.chapters[0]
    with prepared_canon.Session.begin() as session:
        candidate = session.get(CandidateDraftRecord, chapter.candidate_id)
        draft = session.get(ChapterDraft, chapter.draft_id)
        draft.body_text = "Changed content inconsistent with retained accepted plan"
        candidate.body_hash = digest(draft.body_text)
    with (
        prepared_canon.Session.begin() as session,
        pytest.raises(ValueError, match="identity|hash|content"),
    ):
        capture_snapshot(
            session,
            project_id=frozen.project_id,
            book_revision=1,
            display_title=frozen.display_title,
        )


def test_root_swap_after_lock_keeps_all_writes_on_original_directory(
    prepared_canon, tmp_path, monkeypatch
):
    from forwin.novel_export import exporter

    frozen = _accepted(prepared_canon)
    with prepared_canon.Session() as session:
        book = render_snapshot(session, frozen)
    root, outside = tmp_path / "exports", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    original_root = tmp_path / "original-root"
    original = exporter._write_locked

    def swap(held_root, snapshot, text):
        root.rename(original_root)
        root.symlink_to(outside, target_is_directory=True)
        return original(held_root, snapshot, text)

    monkeypatch.setattr(exporter, "_write_locked", swap)
    exporter.write_export(root, frozen, book)
    assert list(outside.iterdir()) == []
    assert (
        original_root / frozen.project_id / "revisions/1/book.md"
    ).read_text() == book
