from __future__ import annotations

import json
from types import SimpleNamespace

from forwin.book_state import BookStateRepository
from forwin.context.assembler_core import assemble_context
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.book_state import WorldNode
from forwin.protocol.context import EntitySnapshot
from forwin.protocol.experience import (
    ArcPayoffMap,
    BandDelightSchedule,
    ChapterExperiencePlan,
    ReaderPromise,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from tests.postgres import postgres_test_url


class _FakeRepo:
    def __init__(self, session=None) -> None:
        self.session = session
        self.project = SimpleNamespace(
            id="project-1",
            title="测试书",
            premise="主角追查天门真相",
            genre="玄幻",
            setting_summary="山海边境",
            creation_status="writing",
            automation_json="{}",
        )
        self.genesis_revision = SimpleNamespace(
            id="genesis-1",
            revision=3,
            pack_json=json.dumps(
                {
                    "world": {
                        "world_bible": {"overview": "天门之后是旧王朝残影"},
                        "map_atlas": {
                            "overview": "三山一城",
                            "submaps": [{"name": "外山"}],
                            "regions": [{"name": "青岚郡", "subworld_name": "外山"}],
                            "nodes": [{"name": "天门渡"}],
                        },
                        "story_engine": {"long_arcs": ["查明天门失火真相"]},
                    }
                },
                ensure_ascii=False,
            ),
        )
        self.entity = EntitySnapshot(
            entity_id="char-1",
            kind="character",
            name="陆沉",
            description="追查天门真相的少年",
            current_state={},
        )

    def get_project(self, project_id: str):
        assert project_id == "project-1"
        return self.project

    def get_active_genesis_revision(self, project_id: str):
        return self.genesis_revision

    def get_allowed_entity_snapshots(self, project_id: str, chapter_number: int):
        return [self.entity]

    def get_active_relations(self, project_id: str, entity_names=None):
        return []

    def get_active_threads(self, project_id: str):
        return []

    def get_chapter_summaries(self, project_id: str, chapter_number: int):
        return ["上一章：陆沉抵达山门。"]

    def get_current_timeline(self, project_id: str):
        return None

    def get_reader_promise(self, project_id: str, *, arc_plan_id=None):
        return ReaderPromise(genre_promise="玄幻网文", core_pleasures=["查谜", "升级"])

    def get_arc_payoff_map(self, project_id: str, *, arc_plan_id=None):
        return ArcPayoffMap(ambiguity_constraints=["不能提前揭示旧王身份"])

    def get_band_experience_plan_for_chapter(self, project_id: str, chapter_number: int):
        return BandDelightSchedule(band_id="band-1", chapter_start=1, chapter_end=3)

    def get_chapter_experience_plan(self, project_id: str, chapter_number: int):
        return ChapterExperiencePlan(planned_reward_tags=["mystery"], progress_markers=["确认线索"])

    def get_active_subworld_summary(self, project_id: str, chapter_number: int, *, runtime=None):
        return []

    def get_active_subworld_region_drafts(self, project_id: str, chapter_number: int, *, runtime=None):
        return [{"name": "临时渡口", "subworld_name": "外山", "level": "1"}]

    def get_audience_hints(self, project_id: str, before_chapter: int):
        return None


    def get_next_band_summary(self, project_id: str, chapter_number: int):
        return None


def test_assemble_context_uses_default_provider_chain() -> None:
    from forwin.context.assembler_core import ChapterContextAssembler

    engine = get_engine(postgres_test_url("context-provider-chain"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            ProjectPolicyStore(session).initialize(
                Project(
                    id="project-1",
                    title="测试书",
                    premise="主角追查天门真相",
                    genre="玄幻",
                    setting_summary="山海边境",
                ),
                RuntimePolicy.for_profile("standard"),
            )
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="char-1",
                    project_id="project-1",
                    node_type="character",
                    name="陆沉",
                    description="追查天门真相的少年",
                    profile={
                        "personality_loadout": {
                            "dominant": {
                                "skill": "trait-loyal-protector",
                                "weight": 0.72,
                            },
                            "secondary": [],
                            "social_mask": [],
                            "stress_modes": [],
                            "relationship_patterns": [],
                            "overrides": {},
                        }
                    },
                ),
                version_chapter=0,
            )
            repo = _FakeRepo(session)
            chapter_plan = _plan(pov_ref="char-1", onstage_refs_json=json.dumps(["char-1"]))

            context = assemble_context(repo, "project-1", chapter_plan)
            provider_names = ChapterContextAssembler().provider_names
    finally:
        engine.dispose()

    assert provider_names == [
        "genesis",
        "state",
        "experience",
        "map",
        "book_state",
        "world_view",
        "personality",
        "feedback",
    ]
    assert context.world_view is not None
    assert (context.world_view.pov_ref, context.world_view.pov_name) == ("char-1", "陆沉")
    assert [item["character_id"] for item in context.active_personality_contexts] == ["char-1"]
    assert context.genesis_context_refs["genesis_revision_id"] == "genesis-1"
    assert context.genesis_world_overview == "天门之后是旧王朝残影"
    assert "临时渡口" in context.genesis_map_overview
    assert context.reader_promise is not None
    assert context.arc_payoff_map is not None
    assert context.band_delight_schedule is not None
    assert context.chapter_experience_plan is not None
    assert context.allowed_entities == ["陆沉"]


def _plan(**fields):
    """A chapter plan with the planner columns the world view reads."""
    payload = {
        "chapter_number": 2,
        "title": "第二章",
        "one_line": "陆沉确认天门有旧王痕迹",
        "goals_json": json.dumps(["找到线索"], ensure_ascii=False),
        "arc_plan_id": "arc-1",
        "hook": "",
        "pov_ref": "",
        "onstage_refs_json": "[]",
        "location_refs_json": "[]",
        "reveal_directives_json": "[]",
        "hint_directives_json": "[]",
        "expected_span_minutes": None,
    }
    return SimpleNamespace(**{**payload, **fields})


def test_world_view_provider_builds_once_with_policy_budget_and_shared_runtime(monkeypatch) -> None:
    from forwin.book_state.runtime import BookStateRuntime
    from forwin.models import ArcPlanVersion
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from forwin.planning.beat_sheet.types import ArcBeatSheetData, BandSpan, BeatPhase
    from forwin.world_view import builder as view_builder
    from forwin.world_view.types import ChapterWorldView

    calls = []

    def build(session, *, project_id, chapter_plan, budget_chars, runtime=None):
        runtime = runtime() if callable(runtime) else runtime
        calls.append((project_id, chapter_plan.chapter_number, budget_chars, runtime))
        return ChapterWorldView(pov_ref="char-1", pov_name="陆沉")

    monkeypatch.setattr(view_builder, "build_chapter_world_view", build)
    engine = get_engine(postgres_test_url("context-world-view-provider"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            ProjectPolicyStore(session).initialize(
                Project(id="project-1", title="测试书", premise="p", genre="玄幻", setting_summary="s"),
                RuntimePolicy.for_profile("pulp"),
            )
            session.add(ArcPlanVersion(id="arc-1", project_id="project-1", arc_number=1, chapter_start=1,
                                       chapter_end=8, arc_synopsis="s", status="active"))
            session.flush()
            ArcBeatSheetRepository(session).create("project-1", "arc-1", ArcBeatSheetData(
                protagonist_ref="char-1",
                phases=[BeatPhase(name="open", chapter_range=(1, 1), purpose="开局"),
                        BeatPhase(name="rise", chapter_range=(2, 5), purpose="陆沉逼近旧王遗迹")],
                band_breakdown=[BandSpan(band_index=0, chapter_start=1, chapter_end=8)],
            ), source="fallback", reason="test")
            context = assemble_context(_FakeRepo(session), "project-1", _plan(hook="门后传来钟声"))
    finally:
        engine.dispose()

    # One build per assembly, on the BookState provider's runtime, within the pulp budget.
    assert [(project, chapter, budget) for project, chapter, budget, _ in calls] == [("project-1", 2, 4000)]
    assert isinstance(calls[0][3], BookStateRuntime)
    assert context.world_view == ChapterWorldView(pov_ref="char-1", pov_name="陆沉")
    assert context.chapter_plan_hook == "门后传来钟声"
    assert context.arc_phase_purpose == "陆沉逼近旧王遗迹"


def test_world_view_failure_fails_the_assembly_and_a_missing_policy_takes_the_default_budget(monkeypatch) -> None:
    import pytest

    from forwin.runtime.policy import WriterPolicy
    from forwin.world_view import builder as view_builder
    from forwin.world_view.types import ChapterWorldView

    budgets = []

    def build(session, *, project_id, chapter_plan, budget_chars, runtime=None):
        budgets.append(budget_chars)
        if chapter_plan.pov_ref == "broken":
            raise RuntimeError("world view failed")
        return ChapterWorldView()

    monkeypatch.setattr(view_builder, "build_chapter_world_view", build)
    engine = get_engine(postgres_test_url("context-world-view-failure"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            # A project row without a stored runtime policy.
            session.add(Project(id="project-1", title="测试书", premise="p", genre="玄幻", setting_summary="s"))
            session.flush()
            context = assemble_context(_FakeRepo(session), "project-1", _plan())
            # The writer never gets a chapter context without its world view.
            with pytest.raises(RuntimeError, match="world view failed"):
                assemble_context(_FakeRepo(session), "project-1", _plan(pov_ref="broken"))
    finally:
        engine.dispose()

    assert context.world_view == ChapterWorldView()
    assert budgets == [WriterPolicy().world_view_budget_chars] * 2 == [6000, 6000]


def test_context_gates_are_explicit_runner_not_provider_side_effects() -> None:
    from forwin.context.gates.context_integrity_gate import ContextIntegrityGate
    from forwin.context.request import ContextDraft, ContextRequest

    request = ContextRequest(
        project_id="project-1",
        chapter_plan=SimpleNamespace(chapter_number=1),
        repo=SimpleNamespace(),
    )
    draft = ContextDraft(data={}, issues=[])

    issues = ContextIntegrityGate().validate(request, draft)

    assert [issue.code for issue in issues] == ["context_project_missing"]
    assert draft.issues == []


def test_context_assembler_has_no_top_level_provider_domain_imports() -> None:
    import inspect

    import forwin.context.assembler_core as assembler_module

    source = inspect.getsource(assembler_module)
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.startswith("from ") or line.startswith("import ")
    ]

    forbidden = (
        "forwin.book_state",
        "forwin.map.",
        "forwin.personality",
        "forwin.world_model.",
    )
    assert not [
        line
        for line in import_lines
        if any(token in line for token in forbidden)
    ]


class _ScalarResult:
    def __init__(self, value: int | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> int | None:
        return self.value


class _MaxChapterSession:
    def __init__(self, value: int | None) -> None:
        self.value = value

    def execute(self, _statement):
        return _ScalarResult(self.value)


def test_canon_quality_context_infers_final_when_target_total_is_stale() -> None:
    from forwin.context.assembler_core import _build_canon_quality_context

    context = _build_canon_quality_context(
        session=_MaxChapterSession(12),
        project_id="project-1",
        chapter_number=12,
        target_total_chapters=0,
        chapter_title="倒计时：最后一日",
        chapter_summary="陆明必须关闭核心系统。",
    )

    assert context["is_final_chapter"] is True


def test_canon_quality_context_does_not_infer_final_before_known_target_total() -> None:
    from forwin.context.assembler_core import _build_canon_quality_context

    context = _build_canon_quality_context(
        session=_MaxChapterSession(12),
        project_id="project-1",
        chapter_number=12,
        target_total_chapters=60,
        chapter_title="倒计时：最后一日",
        chapter_summary="陆明必须关闭核心系统。",
    )

    assert context["is_final_chapter"] is False


def test_canon_quality_context_does_not_infer_ordinary_last_arc_chapter_as_final() -> None:
    from forwin.context.assembler_core import _build_canon_quality_context

    context = _build_canon_quality_context(
        session=_MaxChapterSession(8),
        project_id="project-1",
        chapter_number=8,
        target_total_chapters=0,
        chapter_title="旧轨夹击",
        chapter_summary="陆明被巡检员追击，逃入下一处线索。",
    )

    assert context["is_final_chapter"] is False


def test_active_subworlds_follow_plan_locations(monkeypatch) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from forwin.book_state import BookStateQuery
    from forwin.models import Base, SubWorld, MapRegionRow
    from forwin.models.project import ArcPlanVersion, ChapterPlan
    from forwin.state.repo import StateRepository

    runtime = SimpleNamespace(
        map=SimpleNamespace(nodes_by_id={
            "loc-a": SimpleNamespace(subworld_id="sw-a"),
            "loc-b": SimpleNamespace(subworld_id="sw-b"),
            "loc-c": SimpleNamespace(subworld_id="sw-a"),
        }),
        world=SimpleNamespace(get_state=lambda ref: {"location_id": "loc-b"} if ref == "pov" else {},
                              get_node=lambda ref: None),
    )
    reads = []
    monkeypatch.setattr(BookStateRepository, "latest_canon_chapter", lambda self, project_id: 7)
    def load_runtime(self, project_id, *, as_of_chapter):
        reads.append((project_id, as_of_chapter))
        return runtime
    monkeypatch.setattr(BookStateQuery, "runtime", load_runtime)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id="p", title="title", premise="p", genre="g"))
        session.add(ArcPlanVersion(id="arc", project_id="p", arc_synopsis="arc"))
        for key in ("a", "b", "unused"):
            session.add(SubWorld(id=f"sw-{key}", project_id="p", name=key, scope="global_core"))
            session.add(MapRegionRow(id=f"region-{key}", project_id="p", subworld_id=f"sw-{key}", name=key, region_type="local_region"))
        plan = ChapterPlan(project_id="p", arc_plan_id="arc", chapter_number=8, title="title", pov_ref="pov",
                           location_refs_json='["loc-a", "missing", "loc-c"]',
                           experience_plan_json='{"active_subworld_ids": ["sw-unused"]}')
        session.add(plan)
        session.flush()
        repo = StateRepository(session)
        assert [s.id for s in repo.get_active_subworld_summary("p", 8)] == ["sw-a"]
        assert [r["subworld_id"] for r in repo.get_active_subworld_region_drafts("p", 8)] == ["sw-a"]
        plan.location_refs_json = '["loc-b", "loc-a", "loc-c"]'
        assert {s.id for s in repo.get_active_subworld_summary("p", 8)} == {"sw-a", "sw-b"}
        plan.location_refs_json = "[]"
        assert [s.id for s in repo.get_active_subworld_summary("p", 8)] == ["sw-b"]
        assert [r["subworld_id"] for r in repo.get_active_subworld_region_drafts("p", 8)] == ["sw-b"]
        plan.location_refs_json = '["missing"]'
        assert [s.id for s in repo.get_active_subworld_summary("p", 8)] == ["sw-b"]
        plan.pov_ref = ""
        assert repo.get_active_subworld_summary("p", 8) == []
        assert repo.get_active_subworld_region_drafts("p", 8) == []
        assert repo.get_active_subworld_summary("p", 9) == []
        assert reads and all(item == ("p", 7) for item in reads)
    engine.dispose()
