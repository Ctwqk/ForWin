from __future__ import annotations


def test_review_port_forwards_review_chapter_request() -> None:
    from forwin.review.ports import (
        CallableReviewPort,
        ReviewChapterRequest,
        ReviewChapterResult,
    )

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
    search_request = KnowledgeSearchRequest(
        project_id="project-1", query="secret", role="writer", limit=2
    )

    assert port.rebuild(rebuild_request) == {"rebuilt": "project-1"}
    assert port.search(search_request) == [{"title": "secret"}]
    assert rebuild_calls == [rebuild_request]
    assert search_calls == [search_request]
