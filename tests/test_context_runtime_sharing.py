from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forwin.book_state import BookStateProjection, BookStateRepository
from forwin.book_state.runtime import RuntimeMismatch
from forwin.context.assembler_core import ChapterContextAssembler
from forwin.models import Base, Project, ArcPlanVersion, ChapterPlan
from forwin.protocol.book_state import WorldNode
from forwin.state.repo import StateRepository
from forwin.writer.prompt_core import build_single_chapter_draft_prompt


@pytest.fixture
def chapter_source():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id="p", title="Book", premise="Premise", genre="Genre", setting_summary="Setting"))
        session.add(ArcPlanVersion(id="arc", project_id="p", arc_synopsis="Arc"))
        plan = ChapterPlan(project_id="p", arc_plan_id="arc", chapter_number=1,
                           title="Arrival", one_line="A visitor arrives", goals_json='["Enter"]',
                           pov_ref="actor", onstage_refs_json='["actor"]')
        session.add(plan)
        session.flush()
        BookStateRepository(session).create_world_node(
            WorldNode(id="actor", project_id="p", node_type="character", name="Visitor",
                      profile={"personality_loadout": {"dominant": {"skill": "trait-loyal-protector", "weight": 0.72}}}), version_chapter=0,
        )
        yield session, StateRepository(session), plan
    engine.dispose()


@pytest.mark.parametrize("chapter_number,expected_loads", [(1, 1), (3, 2)])
def test_real_loader_is_shared_per_assembly(chapter_source, monkeypatch, chapter_number, expected_loads):
    session, repo, plan = chapter_source
    plan.chapter_number = chapter_number
    session.flush()
    loads = []
    real_load = BookStateProjection.load_runtime_as_of

    def counted(self, project_id, **kwargs):
        loads.append((project_id, kwargs["as_of_chapter"]))
        return real_load(self, project_id, **kwargs)

    monkeypatch.setattr(BookStateProjection, "load_runtime_as_of", counted)
    pack = ChapterContextAssembler().assemble(repo, "p", plan)
    assert pack.world_view.pov_name == "Visitor"
    assert len(loads) == expected_loads <= 3, loads


@pytest.mark.parametrize("chapter_number", [1, 3])
def test_shared_context_prompt_and_hash_are_identical(chapter_source, chapter_number):
    session, repo, plan = chapter_source
    plan.chapter_number = chapter_number
    session.flush()
    shared = ChapterContextAssembler().assemble(repo, "p", plan)
    independent = ChapterContextAssembler(share_runtime=False).assemble(repo, "p", plan)
    assert shared.model_dump_json() == independent.model_dump_json()
    shared_prompt = build_single_chapter_draft_prompt(shared)
    independent_prompt = build_single_chapter_draft_prompt(independent)
    assert shared_prompt == independent_prompt
    assert hashlib.sha256(shared.model_dump_json().encode()).digest() == hashlib.sha256(independent.model_dump_json().encode()).digest()


def test_world_view_reuses_matching_draft_runtime(chapter_source, monkeypatch):
    from forwin.context.providers.world_view_provider import WorldViewContextProvider
    from forwin.context.request import ContextDraft, ContextRequest

    session, repo, plan = chapter_source
    runtime = BookStateProjection(session).load_runtime_as_of("p", as_of_chapter=0)
    loads = []
    real_load = BookStateProjection.load_runtime_as_of

    def counted(self, project_id, **kwargs):
        loads.append((project_id, kwargs["as_of_chapter"]))
        return real_load(self, project_id, **kwargs)

    monkeypatch.setattr(BookStateProjection, "load_runtime_as_of", counted)
    request = ContextRequest(project_id="p", chapter_plan=plan, repo=repo, session=session)
    draft = ContextDraft(data={"book_state_runtime": runtime})
    WorldViewContextProvider().contribute(request, draft)
    assert draft.data["world_view"].pov_name == "Visitor"
    assert loads == []


def test_world_view_does_not_load_runtime_when_builder_does_not_use_it(monkeypatch):
    from types import SimpleNamespace
    from forwin.context.providers import world_view_provider
    from forwin.context.request import ContextDraft, ContextRequest
    from forwin.world_view import builder
    from forwin.world_view.types import ChapterWorldView

    monkeypatch.setattr(world_view_provider, "_budget_chars", lambda *_: 6000)
    monkeypatch.setattr(world_view_provider, "_phase_purpose", lambda *_: "")
    monkeypatch.setattr(builder, "build_chapter_world_view", lambda *_, **__: ChapterWorldView())
    request = ContextRequest(project_id="p", chapter_plan=SimpleNamespace(chapter_number=1),
                             repo=object(), session=object())
    draft = ContextDraft()
    world_view_provider.WorldViewContextProvider().contribute(request, draft)
    assert draft.data["world_view"] == ChapterWorldView()


@pytest.mark.parametrize("project_id,chapter", [("other", 0), ("p", 1)])
def test_assembly_rejects_mismatched_runtime(chapter_source, project_id, chapter):
    session, repo, plan = chapter_source
    runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=chapter)
    with pytest.raises(RuntimeMismatch):
        ChapterContextAssembler().assemble(repo, "p", plan, runtime=runtime)


@pytest.mark.parametrize("consumer", ["query", "knowledge", "view", "lazy_view", "constraints"])
@pytest.mark.parametrize("project_id,chapter", [("other", 0), ("p", 1)])
def test_consumers_reject_mismatched_runtime(chapter_source, consumer, project_id, chapter):
    from forwin.book_state.query import BookStateQuery
    from forwin.knowledge_system.context import KnowledgeContextQuery
    from forwin.world_view.builder import build_chapter_world_view
    from forwin.context.assembler_core.canon_quality_context import _build_canon_quality_context

    session, repo, plan = chapter_source
    runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=chapter)
    with pytest.raises(RuntimeMismatch):
        if consumer == "query":
            BookStateQuery(session, runtime=runtime).active_entities("p", as_of_chapter=0)
        elif consumer == "knowledge":
            KnowledgeContextQuery(session).build(project_id="p", chapter_number=1, runtime=runtime)
        elif consumer == "view":
            build_chapter_world_view(session, project_id="p", chapter_plan=plan, budget_chars=6000, runtime=runtime)
        elif consumer == "lazy_view":
            build_chapter_world_view(session, project_id="p", chapter_plan=plan, budget_chars=6000, runtime=lambda: runtime)
        else:
            _build_canon_quality_context(session=session, project_id="p", chapter_number=1,
                                         target_total_chapters=10, runtime=runtime)


def test_broker_preserves_prompt_and_supplied_runtime(chapter_source, monkeypatch):
    import json
    from copy import deepcopy
    from types import SimpleNamespace
    from forwin.retrieval.broker_core import broker as broker_module

    session, repo, plan = chapter_source
    runtime = BookStateProjection(session).load_runtime_as_of("p", as_of_chapter=0)
    before = deepcopy((runtime.world.snapshot(), runtime.map.nodes_by_id, runtime.map.edges_by_id, runtime.narrative.snapshot()))
    broker = broker_module.RetrievalBroker(memory_index=SimpleNamespace(search=lambda **kwargs: []))
    shared = broker.build_chapter_context(repo, "p", plan, runtime=runtime)
    monkeypatch.setattr(broker_module, "_assemble_context", lambda repo, project_id, plan: ChapterContextAssembler(share_runtime=False).assemble(repo, project_id, plan))
    independent = broker.build_chapter_context(repo, "p", plan)
    assert shared.model_dump_json() == independent.model_dump_json()
    assert json.dumps(build_single_chapter_draft_prompt(shared), ensure_ascii=False).encode() == json.dumps(build_single_chapter_draft_prompt(independent), ensure_ascii=False).encode()
    assert before == (runtime.world.snapshot(), runtime.map.nodes_by_id, runtime.map.edges_by_id, runtime.narrative.snapshot())
