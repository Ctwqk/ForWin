from __future__ import annotations


def test_book_state_canon_port_forwards_compile() -> None:
    from forwin.book_state.ports import BookStateCanonPort

    calls = []

    class CommitService:
        def compile_approved(self, changes, *, compiler_run_id: str = ""):
            calls.append((changes, compiler_run_id))
            return {"committed": True}

    port = BookStateCanonPort(CommitService())
    result = port.compile("changes", compiler_run_id="run-1")

    assert result == {"committed": True}
    assert calls == [("changes", "run-1")]


def test_review_port_forwards_review_chapter_request() -> None:
    from forwin.reviewer.ports import CallableReviewPort, ReviewChapterRequest, ReviewChapterResult

    calls = []

    def review_chapter(request: ReviewChapterRequest) -> ReviewChapterResult:
        calls.append(request)
        return ReviewChapterResult(verdict={"ok": True}, repair_instruction="retry")

    port = CallableReviewPort(review_chapter)
    request = ReviewChapterRequest(project_id="project-1", chapter_number=7)
    result = port.review_chapter(request)

    assert result.verdict == {"ok": True}
    assert result.repair_instruction == "retry"
    assert calls == [request]


def test_publisher_job_client_forwards_batch_request() -> None:
    from forwin.publisher_runtime.ports import PublisherJobBatchRequest, PublisherRuntimeJobClient

    calls = []

    class Runtime:
        def create_upload_jobs_batch(self, **kwargs):
            calls.append(kwargs)
            return 2

    client = PublisherRuntimeJobClient(Runtime())
    request = PublisherJobBatchRequest(
        project_id="project-1",
        platform="qidian",
        book_name="Book",
        jobs=[{"chapter_title": "第1章", "body": "正文"}],
        upload_url="https://example.test/upload",
        publish=True,
        create_if_missing=True,
        cover_generation_enabled=False,
        publisher_compliance_required=True,
    )

    assert client.create_upload_jobs_batch(request) == 2
    assert calls == [
        {
            "project_id": "project-1",
            "platform": "qidian",
            "book_name": "Book",
            "jobs": [{"chapter_title": "第1章", "body": "正文"}],
            "upload_url": "https://example.test/upload",
            "publish": True,
            "create_if_missing": True,
            "book_meta": None,
            "cover_generation_enabled": False,
            "cover_confirmation_required": False,
            "cover_candidate_count": 4,
            "cover_style_hint": "",
            "auto_cover_upload_enabled": True,
            "publisher_compliance_required": True,
        }
    ]


def test_knowledge_index_port_forwards_rebuild_and_search_requests() -> None:
    from forwin.retrieval.ports import (
        CallableKnowledgeIndexPort,
        KnowledgeRebuildRequest,
        KnowledgeSearchRequest,
    )

    rebuild_calls = []
    search_calls = []

    def rebuild(request: KnowledgeRebuildRequest):
        rebuild_calls.append(request)
        return {"rebuilt": request.project_id}

    def search(request: KnowledgeSearchRequest):
        search_calls.append(request)
        return [{"title": request.query}]

    port = CallableKnowledgeIndexPort(rebuild=rebuild, search=search)
    rebuild_request = KnowledgeRebuildRequest(project_id="project-1", as_of_chapter=3)
    search_request = KnowledgeSearchRequest(project_id="project-1", query="secret", role="writer", limit=2)

    assert port.rebuild(rebuild_request) == {"rebuilt": "project-1"}
    assert port.search(search_request) == [{"title": "secret"}]
    assert rebuild_calls == [rebuild_request]
    assert search_calls == [search_request]
