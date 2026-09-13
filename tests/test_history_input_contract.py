from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.knowledge_system.context import KnowledgeContextQuery
from forwin.knowledge_system.dependencies import page_dependencies
from forwin.knowledge_system.store import KnowledgeProjectionStore
from forwin.models.base import get_engine, get_session_factory
from forwin.models.knowledge import KnowledgeProjectionPageRow
from forwin.models.project import Project
from forwin.obsidian.exporter import ObsidianExporter
from forwin.obsidian.frontmatter import render_page
from forwin.protocol.book_state import MapNode, WorldNode
from forwin.protocol.context import ChapterContextPack, MemorySnippet
from forwin.protocol.scene import ScenePlan
from forwin.protocol.world_model import WorldContextPack, WorldModelPage
from forwin.protocol.writer import WriterOutput
from forwin.retrieval.broker_core.broker import RetrievalBroker
from forwin.review.context_builder import build_review_context_pack
from forwin.review.llm_webnovel import LLMWebNovelReviewer
from forwin.writer.prompt_core.builders import (
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
    build_single_chapter_draft_prompt,
)
from tests.postgres import postgres_test_url


def _context() -> ChapterContextPack:
    return ChapterContextPack(
        project_id="history-contract",
        project_title="归航",
        premise="核查一次航行记录。",
        genre="悬疑",
        setting_summary="港口",
        chapter_number=9,
        chapter_plan_title="复核",
        chapter_plan_one_line="继续核查记录。",
        chapter_goals=[],
        previous_chapter_summaries=["船只昨日入港。", "记录已经封存。"],
        retrieved_memories=[
            MemorySnippet(
                chapter_number=n,
                title=f"记录{n}",
                summary=f"已核实的记录{n}",
                excerpt=f"正文证据{n}",
            )
            for n in (2, 7)
        ],
    )


def _prompts(context: ChapterContextPack) -> dict[str, list[dict]]:
    return {
        "single": build_single_chapter_draft_prompt(context),
        "preview": build_preview_chapter_prompt(context),
        "breakdown": build_scene_breakdown_prompt(context),
        "scene": build_scene_generation_prompt(
            context, ScenePlan(scene_no=1, objective="继续调查")
        ),
        "stitch": build_scene_stitch_prompt(context, []),
    }


@pytest.mark.parametrize("budget", [3000, 6000])
def test_invisible_provenance_cannot_evict_visible_history(budget: int) -> None:
    broker = RetrievalBroker(context_budget_chars=budget)
    context = _context()
    inflated = context.model_copy(update={"knowledge_system_context": {
        "knowledge_system_v46": {"book_state_snapshot": {"audit": "x" * 65000}},
    }})
    original = inflated.model_dump(mode="json")
    assert _prompts(inflated) == _prompts(context)

    ordinary = broker._trim_pack(context)
    with_provenance = broker._trim_pack(inflated)

    assert with_provenance.previous_chapter_summaries == ordinary.previous_chapter_summaries
    assert with_provenance.retrieved_memories == ordinary.retrieved_memories
    assert _prompts(with_provenance) == _prompts(ordinary)
    assert with_provenance.knowledge_system_context == inflated.knowledge_system_context
    assert inflated.model_dump(mode="json") == original


def test_reviewer_consumes_complete_retained_history_with_citable_evidence() -> None:
    context = _context()
    # Source strings have no absolute chapter identity; do not infer contiguous chapters.
    context.previous_chapter_summaries = [
        "先前巡查发现灯塔熄灭。" * 20 + "灯塔钥匙已交给船长。",
        "船长将钥匙封入档案袋。",
    ]
    review_context = build_review_context_pack(context=context)
    output = WriterOutput(
        chapter_number=9, title="复核", body="船长没有收到过钥匙。",
        end_of_chapter_summary="船长否认接收钥匙。",
    )
    reviewer = LLMWebNovelReviewer(enabled=False)

    payload = reviewer._llm_payload(review_context, output)
    evidence_ids = {item["evidence_id"] for item in payload["evidence_index"]}
    messages = reviewer._llm_review_messages(payload=payload, evidence_ids=sorted(evidence_ids))
    prompt = "\n".join(item["content"] for item in messages)

    for summary in context.previous_chapter_summaries:
        assert summary in prompt
    assert payload["previous_chapter_summaries"] == [
        {"evidence_id": f"history:summary:{index}", "summary": summary}
        for index, summary in enumerate(context.previous_chapter_summaries, 1)
    ]
    assert {"history:summary:1", "history:summary:2"} <= evidence_ids
    assert "history:summary:8" not in evidence_ids
    changed = review_context.model_copy(update={"previous_chapter_summaries": ["不同历史。"]})
    assert reviewer._llm_payload(changed, output) != payload
    assert context.previous_chapter_summaries == review_context.previous_chapter_summaries

    verdict = reviewer._verdict_from_payload(
        payload={"verdict": "fail", "issues": [{
            "rule_name": "history_conflict", "severity": "error",
            "description": "已接收钥匙，却无解释地否认接收。", "issue_type": "continuity",
            "target_scope": "chapter", "evidence_refs": ["history:summary:1", "draft:body"],
        }]},
        context=review_context, writer_output=output,
        fallback_on_invalid=False, allowed_evidence_ids=evidence_ids,
    )
    assert verdict.verdict == "fail"
    assert verdict.issues[0].evidence_refs == ["history:summary:1", "draft:body"]
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def _world_page(**frontmatter_update: str) -> WorldModelPage:
    frontmatter = {
        "forwin_id": "character:captain", "node_type": "character",
        "node_id": "captain",
        "project_id": "history-contract", "as_of_chapter": 7,
        "source_digest": "FRONTMATTER_ONLY" * 30,
        "visibility": "reader_known", "truth_relation": "true",
        **frontmatter_update,
    }
    return WorldModelPage(
        page_key="character:captain", page_type="character", title="船长",
        frontmatter=frontmatter,
        markdown=render_page(frontmatter, "船长", {
            "Canon Summary": "船长接收了钥匙。",
            "Current State": "钥匙已放入封存袋。",
            "Manual Notes": "MANUAL_UNACCEPTED",
            "Proposed Correction": "PROPOSED_UNACCEPTED",
            "Open Questions": "UNRESOLVED_QUESTION",
        }),
    )


def _with_page(page: WorldModelPage) -> ChapterContextPack:
    return _context().model_copy(update={"world_context": WorldContextPack(
        snapshot_id="snapshot-seven", as_of_chapter=7, relevant_world_pages=[page],
    )})


@pytest.mark.parametrize("mode", ["single", "preview", "breakdown", "scene", "stitch"])
def test_writer_renders_accepted_world_page_state_without_authoring_content(mode: str) -> None:
    context = _with_page(_world_page())
    original = context.model_dump(mode="json")

    prompt = "\n".join(item["content"] for item in _prompts(context)[mode])

    assert "船长接收了钥匙。" in prompt
    assert "钥匙已放入封存袋。" in prompt
    for excluded in ("FRONTMATTER_ONLY", "MANUAL_UNACCEPTED", "PROPOSED_UNACCEPTED", "UNRESOLVED_QUESTION"):
        assert excluded not in prompt
    assert context.model_dump(mode="json") == original


@pytest.mark.parametrize("hidden", [
    {"visibility": "hidden"}, {"visibility": "secret"},
    {"visibility": "must_not_reveal"}, {"truth_relation": "hidden"},
    {"truth_relation": "secret"}, {"status": "hidden"},
    {"status": "secret"}, {"node_type": "secret"},
])
@pytest.mark.parametrize("metadata_source", ["both", "markdown_only", "disagreement"])
def test_writer_does_not_render_hidden_pages_or_their_promise_titles(
    hidden: dict[str, str], metadata_source: str,
) -> None:
    page = _world_page(**hidden).model_copy(update={"title": "HIDDEN_PAGE_TITLE"})
    if metadata_source == "markdown_only":
        page.frontmatter = {}
    elif metadata_source == "disagreement":
        page.frontmatter = {"visibility": "reader_known", "truth_relation": "true"}
    context = _with_page(page)
    context.world_context.active_promises = [page]
    context.world_context.active_secrets = [page]

    for messages in _prompts(context).values():
        prompt = "\n".join(item["content"] for item in messages)
        assert "船长接收了钥匙。" not in prompt
        assert "钥匙已放入封存袋。" not in prompt
        assert "HIDDEN_PAGE_TITLE" not in prompt
        assert "不得提前揭示" in prompt


@pytest.mark.parametrize("page_fields", [{"page_type": "secret"}, {"status": "secret"}])
def test_writer_honors_page_identity_when_frontmatter_is_missing(page_fields: dict[str, str]) -> None:
    page = _world_page().model_copy(update={
        **page_fields, "frontmatter": {},
        "markdown": "## Canon Summary\nHIDDEN_SUMMARY\n## Current State\nHIDDEN_STATE",
    })
    for messages in _prompts(_with_page(page)).values():
        prompt = "\n".join(item["content"] for item in messages)
        assert "HIDDEN_SUMMARY" not in prompt
        assert "HIDDEN_STATE" not in prompt


@pytest.mark.parametrize("visibility", ["reader_known", "revealed"])
@pytest.mark.parametrize("truth", ["true", "false", "unknown"])
def test_visible_world_page_retains_truth_and_visibility_qualifiers(visibility: str, truth: str) -> None:
    page = _world_page(visibility=visibility, truth_relation=truth)
    for messages in _prompts(_with_page(page)).values():
        prompt = "\n".join(item["content"] for item in messages)
        assert "钥匙已放入封存袋。" in prompt
        assert f"visibility={visibility}" in prompt
        assert f"truth_relation={truth}" in prompt


def test_world_page_summary_cannot_crowd_out_state_and_both_are_bounded() -> None:
    page = _world_page()
    page.markdown = render_page(page.frontmatter, page.title, {
        "Canon Summary": "SUMMARY_HEAD" + "x" * 400 + "SUMMARY_TAIL",
        "Current State": "STATE_HEAD" + "y" * 400 + "STATE_TAIL",
    })
    for messages in _prompts(_with_page(page)).values():
        prompt = "\n".join(item["content"] for item in messages)
        assert "SUMMARY_HEAD" in prompt and "STATE_HEAD" in prompt
        assert "SUMMARY_TAIL" not in prompt and "STATE_TAIL" not in prompt


class _CaptureExporter(ObsidianExporter):
    def __init__(self) -> None:
        self.page: WorldModelPage | None = None

    def _write_page(self, root, rel_path, title, frontmatter, sections, *, page_type):
        self.page = WorldModelPage(
            page_key=frontmatter["forwin_id"], page_type=page_type, title=title,
            frontmatter=frontmatter, markdown=render_page(frontmatter, title, sections),
        )


@pytest.mark.parametrize("source_hidden", [
    {"status": "hidden"}, {"status": "secret"}, {"status": "must_not_reveal"},
    {"tags": ["hidden"]}, {"tags": ["secret"]}, {"tags": ["must_not_reveal"]},
    {"metadata": {"visibility": "hidden", "reader_visibility": "reader_known"}},
])
def test_exported_node_preserves_canonical_hidden_status_and_tags(source_hidden: dict) -> None:
    node = WorldNode(
        id="hidden-captain", project_id="history-contract", node_type="character",
        name="船长", summary="航行档案", state={"identity": "SOURCE_HIDDEN_STATE"},
        **source_hidden,
    )
    original = node.model_dump(mode="json")
    exporter = _CaptureExporter()
    exporter._write_node_page(Path("unused"), node.project_id, "unused.md", node, {}, 7)
    assert exporter.page is not None

    for messages in _prompts(_with_page(exporter.page)).values():
        assert "SOURCE_HIDDEN_STATE" not in "\n".join(item["content"] for item in messages)
    assert node.model_dump(mode="json") == original


@pytest.mark.parametrize("source_hidden", [
    {"status": "hidden"}, {"tags": ["must_not_reveal"]},
    {"metadata": {"reader_visibility": "hidden"}},
    {"metadata": {"truth_relation": "hidden"}},
    {"metadata": {"truth_relation": "secret"}},
])
def test_existing_page_visibility_uses_as_of_canon_without_rewriting_projection(
    source_hidden: dict, history_session,
) -> None:
    node = WorldNode(
        id="captain", project_id="history-contract", node_type="character", **source_hidden,
    )
    page = _world_page().model_copy(update={
        "canonical_source_type": "book_state_node", "canonical_source_id": node.id,
    })
    query = _query_pages(history_session, [page], world_nodes={node.id: node})
    stored = history_session.scalar(select(KnowledgeProjectionPageRow))
    original = (stored.markdown, stored.frontmatter_json, stored.content_hash)
    world = query.build(project_id="history-contract", chapter_number=8)

    context = _context().model_copy(update={"world_context": world})
    for messages in _prompts(context).values():
        assert "钥匙已放入封存袋。" not in "\n".join(item["content"] for item in messages)
    assert world.as_of_chapter == 7
    history_session.refresh(stored)
    assert (stored.markdown, stored.frontmatter_json, stored.content_hash) == original
    # Reviewer still receives original full content, with corrected visibility.
    assert world.relevant_world_pages[0].markdown == page.markdown


@pytest.fixture
def history_session():
    engine = get_engine(postgres_test_url("history-input-contract"))
    with get_session_factory(engine)() as session:
        session.add(Project(id="history-contract", title="归航", premise="港口"))
        session.commit()
        yield session
    engine.dispose()


def _query_pages(session, pages, *, world_nodes=None, map_nodes=None):
    repository = BookStateRepository(session)
    if world_nodes is None:
        world_nodes = {"captain": WorldNode(id="captain", project_id="history-contract", node_type="character", name="船长")}
    for node in world_nodes.values():
        repository.create_world_node(node)
    for node in (map_nodes or {}).values():
        repository.create_map_node(node)
    projection = BookStateProjection(session)
    runtime = projection.load_runtime_as_of("history-contract", as_of_chapter=7)
    projection.persist_snapshots(runtime, as_of_chapter=7)
    for page in pages:
        node_id = page.canonical_source_id or str(page.frontmatter.get("node_id") or "")
        scope = "book" if page.page_type == "book" else "map_node" if page.page_type == "map_node" else "node"
        manifest = page_dependencies(runtime, scope=scope, node_id=node_id,
            extra={"project_title": "归航"} if scope == "book" else None)
        KnowledgeProjectionStore(session).upsert_page(
            project_id="history-contract", page_key=page.page_key, page_type=page.page_type,
            title=page.title, vault_path=page.vault_path, markdown=page.markdown,
            frontmatter=page.frontmatter, as_of_chapter=page.as_of_chapter,
            dependency_manifest=manifest,
        )
    session.commit()
    return KnowledgeContextQuery(session)


def test_page_selection_excludes_future_state_before_limit(history_session) -> None:
    prior = _world_page().model_copy(update={"as_of_chapter": 7})
    future = _world_page().model_copy(update={
        "page_key": "character:future", "title": "未来命中词", "as_of_chapter": 8,
        "frontmatter": {**prior.frontmatter, "node_id": "future"},
        "markdown": "## Current State\nFUTURE_STATE",
    })
    query = _query_pages(history_session, [future, prior], world_nodes={
        name: WorldNode(id=name, project_id="history-contract", node_type="character", name=name)
        for name in ["captain", "future"]
    })

    world = query.build(
        project_id="history-contract", chapter_number=8, query_terms=["未来命中词"], max_pages=1,
    )

    assert [page.page_key for page in world.relevant_world_pages] == [prior.page_key]
    assert "FUTURE_STATE" not in world.model_dump_json()


def test_missing_canonical_node_cannot_make_projection_visible(history_session) -> None:
    page = _world_page().model_copy(update={
        "canonical_source_type": "book_state_node", "canonical_source_id": "missing",
    })
    query = _query_pages(history_session, [page])
    world = query.build(project_id="history-contract", chapter_number=8)
    for messages in _prompts(_context().model_copy(update={"world_context": world})).values():
        assert "钥匙已放入封存袋。" not in "\n".join(item["content"] for item in messages)


@pytest.mark.parametrize("hidden", [True, False])
def test_map_projection_uses_map_source_visibility(history_session, hidden: bool) -> None:
    node = MapNode(
        id="dock", project_id="history-contract", node_type="site", name="泊位",
        status="hidden" if hidden else "normal", description="MAP_SOURCE_DESCRIPTION",
    )
    exporter = _CaptureExporter()
    exporter._write_map_node_page(Path("unused"), node.project_id, "unused.md", node, {}, 7)
    page = exporter.page
    assert page is not None
    for messages in _prompts(_with_page(page)).values():
        assert ("MAP_SOURCE_DESCRIPTION" in "\n".join(item["content"] for item in messages)) is not hidden

    # An old page claiming visibility must still resolve against the map owner.
    page.frontmatter["visibility"] = "reader_known"
    page.markdown = render_page(page.frontmatter, page.title, {"Current State": "MAP_SOURCE_STATE"})
    query = _query_pages(history_session, [page], map_nodes={node.id: node})
    world = query.build(project_id="history-contract", chapter_number=8)
    for messages in _prompts(_context().model_copy(update={"world_context": world})).values():
        assert ("MAP_SOURCE_STATE" in "\n".join(item["content"] for item in messages)) is not hidden


def test_book_aggregate_does_not_expose_mixed_visibility_state(history_session) -> None:
    page = _world_page(node_type="book", node_id="book:reader-promise-ledger").model_copy(
        update={"page_type": "book"}
    )
    page.markdown = render_page(page.frontmatter, page.title, {
        "Canon Summary": "BOOK_SUMMARY", "Current State": "HIDDEN_AGGREGATED_PROMISE",
    })
    query = _query_pages(history_session, [page])
    world = query.build(project_id="history-contract", chapter_number=8)
    for messages in _prompts(_context().model_copy(update={"world_context": world})).values():
        prompt = "\n".join(item["content"] for item in messages)
        assert "BOOK_SUMMARY" in prompt
        assert "HIDDEN_AGGREGATED_PROMISE" not in prompt


@pytest.mark.parametrize("page_type", ["overview", "legacy_aggregate", "character"])
def test_state_requires_individual_page_type_and_source_identity(page_type: str) -> None:
    page = _world_page().model_copy(update={"page_type": page_type})
    if page_type == "character":
        page.frontmatter.pop("node_id")
    page.markdown = render_page(page.frontmatter, page.title, {
        "Canon Summary": "SOURCE_SUMMARY", "Current State": "UNRESOLVED_SOURCE_STATE",
    })
    for messages in _prompts(_with_page(page)).values():
        prompt = "\n".join(item["content"] for item in messages)
        assert "SOURCE_SUMMARY" in prompt
        assert "UNRESOLVED_SOURCE_STATE" not in prompt
