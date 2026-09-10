"""MCP proposals retain the accepted body and expose optimistic version identity."""

import json
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService
from forwin.config import InfrastructureConfig
from forwin.mcp.client import ForWinAPIClient
from forwin.mcp.http import build_mcp_server
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan, Project
from tests import test_canon_atomic_transaction as atomic_tests
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.test_mcp_runtime_policy import call, tools

prepared_canon = atomic_tests.prepared_canon


@pytest.fixture
def runtime(prepared_canon):
    fixture = prepared_canon
    accepted = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    engine = fixture.Session.kw["bind"]
    harness = HttpRuntimeHarness(
        session_factory=fixture.Session,
        engine=engine,
        config=InfrastructureConfig(database_url=str(engine.url)),
    )
    client = ForWinAPIClient(
        base_url="http://forwin.test", transport=httpx.ASGITransport(app=harness.app)
    )
    return SimpleNamespace(
        fixture=fixture, accepted=accepted, server=build_mcp_server(api_client=client)
    )


def retry(runtime, **fields):
    return call(
        runtime.server,
        "chapter_review_retry",
        {
            "project_id": runtime.fixture.project_id,
            "chapter_number": 1,
            "reason": "Isolated revision proposal test",
            "allow_accepted": True,
            **fields,
        },
    )


def candidate_ids(runtime):
    with runtime.fixture.Session() as session:
        return set(session.scalars(select(CandidateDraftRecord.id)))


def assert_mainline_unchanged(runtime):
    fixture = runtime.fixture
    with fixture.Session() as session:
        chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
        assert chapter.active_commit_id == runtime.accepted.commit_id
        assert chapter.status == "accepted"
        assert session.get(Project, fixture.project_id).book_revision == 1


def test_mcp_revision_input_and_identity_output_schema():
    catalog = tools(
        build_mcp_server(api_client=ForWinAPIClient(base_url="http://invalid"))
    )
    retry_tool = catalog["chapter_review_retry"]
    assert {"replacement_body", "replacement_title", "expected_book_revision"} <= (
        retry_tool.inputSchema["properties"].keys()
    )
    assert {"candidate_id", "book_revision"} <= retry_tool.outputSchema[
        "properties"
    ].keys()
    assert "book_revision" in catalog["project_get"].outputSchema["properties"]


def test_mcp_project_detail_and_list_expose_current_book_revision(runtime):
    project_id = runtime.fixture.project_id
    project = call(runtime.server, "project_get", {"project_id": project_id})
    assert project["book_revision"] == 1
    projects = call(runtime.server, "project_list", {})["projects"]
    assert next(p for p in projects if p["id"] == project_id)["book_revision"] == 1


def test_mcp_replacement_creates_distinct_proposal_with_full_body_and_title(runtime):
    before = candidate_ids(runtime)
    body = "Shen Linchuan quietly enters the archive.\nAn unchanged shelf waits."
    result = retry(
        runtime,
        replacement_body=body,
        replacement_title="The quiet archive",
        expected_book_revision=1,
    )
    assert result["candidate_id"] not in before
    assert candidate_ids(runtime) == before | {result["candidate_id"]}
    assert result["book_revision"] == result["project"]["book_revision"] == 1
    with runtime.fixture.Session() as session:
        candidate = session.get(CandidateDraftRecord, result["candidate_id"])
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        assert draft.body_text == body
        assert json.loads(candidate.metadata_json)["title"] == "The quiet archive"
        assert candidate.status == "drafted"
        assert candidate.state_change_candidates_json == "[]"
    assert_mainline_unchanged(runtime)
    accepted = call(
        runtime.server,
        "chapter_get",
        {"project_id": runtime.fixture.project_id, "chapter_number": 1},
    )
    assert accepted["chapter_plan_id"] == runtime.fixture.chapter_plan_id
    assert accepted["active_commit_id"] == runtime.accepted.commit_id
    assert accepted["candidate_id"] == runtime.fixture.candidate_id
    assert accepted["book_revision"] == accepted["acceptance_revision"] == 1
    assert accepted["body"] == "Shen Linchuan enters the archive."
    assert accepted["body_sha256"] == sha256(accepted["body"].encode()).hexdigest()
    assert accepted["title"] == "Chapter one"


def test_mcp_stale_proposal_rejects_without_creating_candidate(runtime):
    before = candidate_ids(runtime)
    with pytest.raises(ToolError, match="409.*book revision.*stale"):
        retry(runtime, replacement_body="Stale wording.", expected_book_revision=0)
    assert candidate_ids(runtime) == before
    assert_mainline_unchanged(runtime)


def test_mcp_bare_accepted_retry_records_request_without_inventing_body(runtime):
    before = candidate_ids(runtime)
    result = retry(runtime)
    assert result["candidate_id"] == ""
    assert result["book_revision"] == 1
    assert candidate_ids(runtime) == before
    assert_mainline_unchanged(runtime)


def test_mcp_chapter_body_and_identity_share_one_revision_during_concurrent_commit(
    runtime, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from forwin.application.projects import chapters

    started = Event()
    committed = Event()
    original = chapters.load_latest_drafts_by_plan_id

    def advance_book_revision():
        with runtime.fixture.Session.begin() as session:
            started.set()
            project = session.scalar(
                select(Project)
                .where(Project.id == runtime.fixture.project_id)
                .with_for_update()
            )
            project.book_revision = 2
        committed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []

        def read_draft(*args, **kwargs):
            futures.append(pool.submit(advance_book_revision))
            assert started.wait(2)
            # A real concurrent Project owner must wait for this read snapshot.
            assert not committed.wait(0.1)
            return original(*args, **kwargs)

        monkeypatch.setattr(chapters, "load_latest_drafts_by_plan_id", read_draft)
        result = call(
            runtime.server,
            "chapter_get",
            {"project_id": runtime.fixture.project_id, "chapter_number": 1},
        )
        assert result["book_revision"] == 1
        assert result["active_commit_id"] == runtime.accepted.commit_id
        futures[0].result(timeout=2)
    assert committed.is_set()


def test_legacy_accepted_without_pointer_refuses_formal_body_and_retains_history(
    runtime,
):
    retry(runtime, replacement_body="Unaccepted replacement is not formal history.")
    with runtime.fixture.Session.begin() as session:
        session.get(
            ChapterPlan, runtime.fixture.chapter_plan_id
        ).active_commit_id = None
        session.get(Project, runtime.fixture.project_id).book_revision = 0
        draft_ids = set(session.scalars(select(ChapterDraft.id)))
        review_ids = set(session.scalars(select(ChapterReview.id)))
    with pytest.raises(ToolError, match="409.*Canon.*身份不完整"):
        call(
            runtime.server,
            "chapter_get",
            {"project_id": runtime.fixture.project_id, "chapter_number": 1},
        )
    chapters = call(
        runtime.server,
        "chapter_list",
        {"project_id": runtime.fixture.project_id},
    )["chapters"]
    assert chapters[0]["status"] == "accepted"
    assert chapters[0]["char_count"] == 0
    with runtime.fixture.Session() as session:
        assert set(session.scalars(select(ChapterDraft.id))) == draft_ids
        assert set(session.scalars(select(ChapterReview.id))) == review_ids
        session.scalar(
            select(Project)
            .where(Project.id == runtime.fixture.project_id)
            .with_for_update(nowait=True)
        )


@pytest.mark.parametrize("status", ["draft", "planned"])
def test_unaccepted_chapter_get_preserves_candidate_body_and_missing_draft_semantics(
    runtime, status
):
    with runtime.fixture.Session.begin() as session:
        first = session.get(ChapterPlan, runtime.fixture.chapter_plan_id)
        chapter = ChapterPlan(
            project_id=first.project_id,
            arc_plan_id=first.arc_plan_id,
            chapter_number=2,
            title="Unaccepted chapter",
            status=status,
        )
        session.add(chapter)
        session.flush()
        if status == "draft":
            session.add_all(
                [
                    ChapterDraft(
                        chapter_plan_id=chapter.id,
                        version=1,
                        body_text="First candidate",
                    ),
                    ChapterDraft(
                        chapter_plan_id=chapter.id,
                        version=2,
                        body_text="Repaired candidate",
                    ),
                ]
            )
    arguments = {"project_id": runtime.fixture.project_id, "chapter_number": 2}
    if status == "draft":
        result = call(runtime.server, "chapter_get", arguments)
        assert result["body"] == "Repaired candidate"
        assert result["active_commit_id"] == result["candidate_id"] == ""
    else:
        with pytest.raises(ToolError, match="404.*尚未生成"):
            call(runtime.server, "chapter_get", arguments)


def test_hash_mismatch_refuses_identity_and_releases_shared_lock(runtime):
    with runtime.fixture.Session.begin() as session:
        draft = session.scalar(
            select(ChapterDraft).where(
                ChapterDraft.chapter_plan_id == runtime.fixture.chapter_plan_id
            )
        )
        draft.body_text = "Body no longer matches its accepted evidence."
    with pytest.raises(ToolError, match="409.*Canon"):
        call(
            runtime.server,
            "chapter_get",
            {"project_id": runtime.fixture.project_id, "chapter_number": 1},
        )
    with runtime.fixture.Session.begin() as session:
        project = session.scalar(
            select(Project)
            .where(Project.id == runtime.fixture.project_id)
            .with_for_update(nowait=True)
        )
        assert project.id == runtime.fixture.project_id
