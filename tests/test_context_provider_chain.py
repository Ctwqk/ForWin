from __future__ import annotations

import json
from types import SimpleNamespace

from forwin.context.assembler import assemble_context
from forwin.protocol.context import EntitySnapshot
from forwin.protocol.experience import (
    ArcPayoffMap,
    BandDelightSchedule,
    ChapterExperiencePlan,
    ReaderPromise,
)


class _FakeRepo:
    session = None

    def __init__(self) -> None:
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

    def get_recent_npc_intents(self, project_id: str, before_chapter: int):
        return []

    def get_latest_world_pressure(self, project_id: str, before_chapter: int):
        return None

    def get_active_arc_envelope(self, project_id: str):
        return None

    def get_reader_promise(self, project_id: str):
        return ReaderPromise(genre_promise="玄幻网文", core_pleasures=["查谜", "升级"])

    def get_arc_payoff_map(self, project_id: str):
        return ArcPayoffMap(ambiguity_constraints=["不能提前揭示旧王身份"])

    def get_band_experience_plan_for_chapter(self, project_id: str, chapter_number: int):
        return BandDelightSchedule(band_id="band-1", chapter_start=1, chapter_end=3)

    def get_chapter_experience_plan(self, project_id: str, chapter_number: int):
        return ChapterExperiencePlan(planned_reward_tags=["mystery"], progress_markers=["确认线索"])

    def get_active_subworld_summary(self, project_id: str, chapter_number: int):
        return []

    def get_active_subworld_region_drafts(self, project_id: str, chapter_number: int):
        return [{"name": "临时渡口", "subworld_name": "外山", "level": "1"}]

    def get_audience_hints(self, project_id: str, before_chapter: int):
        return None

    def get_chapter_task_contract(self, project_id: str, chapter_number: int):
        return []

    def get_band_task_contract_for_chapter(self, project_id: str, chapter_number: int):
        return []

    def future_constraints_enabled(self, project_id: str):
        return False

    def list_active_narrative_constraints(self, project_id: str, chapter_number: int):
        return []

    def get_next_band_summary(self, project_id: str, chapter_number: int):
        return None


def test_assemble_context_uses_default_provider_chain() -> None:
    from forwin.context.assembler import ChapterContextAssembler

    repo = _FakeRepo()
    chapter_plan = SimpleNamespace(
        chapter_number=2,
        title="第二章",
        one_line="陆沉确认天门有旧王痕迹",
        goals_json=json.dumps(["找到线索"], ensure_ascii=False),
        arc_plan_id="arc-1",
    )

    context = assemble_context(repo, "project-1", chapter_plan)
    provider_names = ChapterContextAssembler().provider_names

    assert provider_names == [
        "genesis",
        "state",
        "experience",
        "map",
        "book_state",
        "personality",
        "feedback",
    ]
    assert context.genesis_context_refs["genesis_revision_id"] == "genesis-1"
    assert context.genesis_world_overview == "天门之后是旧王朝残影"
    assert "临时渡口" in context.genesis_map_overview
    assert context.reader_promise is not None
    assert context.arc_payoff_map is not None
    assert context.band_delight_schedule is not None
    assert context.chapter_experience_plan is not None
    assert context.allowed_entities == ["陆沉"]


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

    import forwin.context.assembler as assembler_module

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
    from forwin.context.assembler import _build_canon_quality_context

    context = _build_canon_quality_context(
        session=_MaxChapterSession(12),
        project_id="project-1",
        chapter_number=12,
        target_total_chapters=0,
        chapter_title="倒计时：最后一日",
        chapter_summary="林澈必须关闭白塔系统。",
    )

    assert context["is_final_chapter"] is True


def test_canon_quality_context_does_not_infer_ordinary_last_arc_chapter_as_final() -> None:
    from forwin.context.assembler import _build_canon_quality_context

    context = _build_canon_quality_context(
        session=_MaxChapterSession(8),
        project_id="project-1",
        chapter_number=8,
        target_total_chapters=0,
        chapter_title="旧轨夹击",
        chapter_summary="林澈被巡检员追击，逃入下一处线索。",
    )

    assert context["is_final_chapter"] is False
