from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.api_schema.review import BandExperienceOverrideRequest
from forwin.application.project_control.operations import override_band_experience
from forwin.context.providers.experience_provider import ExperienceContextProvider
from forwin.context.request import ContextDraft, ContextRequest
from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.projection import writer_payoff_map
from forwin.generation.pipeline_core.repair_patches import RepairPlanningStage
from forwin.models import Base
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import ArcBeatSheetData
from forwin.protocol.experience import ArcPayoffMap, RevelationLayer
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.repo import StateRepository
from forwin.world_view.types import ChapterWorldView
from tests.test_writer_world_view_prompt import BUILDERS, _context, _prompt


SECRET = "HiddenKeeper concealed the original charter"
SAFE_PROMISE = "A visible advance at a clear cost"


@pytest.fixture
def experience_book():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session.begin() as session:
        ProjectPolicyStore(session).initialize(
            Project(id="p", title="title", premise="premise", genre="test"), RuntimePolicy.for_profile("standard"),
        )
        session.add(ArcPlanVersion(id="arc", project_id="p", chapter_start=1, chapter_end=8,
                                   arc_synopsis="synopsis", status="active"))
        session.flush()
        data = ArcBeatSheetData.model_validate({
            "protagonist_ref": "hero",
            "phases": [{"name": "phase", "chapter_range": [1, 8], "purpose": "Follow the evidence"}],
            "band_breakdown": [{"band_index": 0, "chapter_start": 1, "chapter_end": 8}],
            "reader_promise": {"pleasure_promise": SAFE_PROMISE},
            "arc_payoff_map": {
                "revelation_layers": [{"summary": SECRET, "chapter_window": "8"}],
                "ambiguity_constraints": ["Every inference needs evidence"],
            },
        })
        ArcBeatSheetRepository(session).create("p", "arc", data, source="llm", reason="test")
        for number in (1, 8):
            session.add(ChapterPlan(project_id="p", arc_plan_id="arc", chapter_number=number,
                                    title="title", one_line="Follow the clue", goals_json='["Find evidence"]'))
        experience = StateRepository(session).get_beat_sheet_experience("p")
        schedule = BandExperienceScheduler().derive_band_delight_schedule(
            band_id="band:1:8", chapter_start=1, chapter_end=8, experience=experience, active_band=[],
        )
        session.add(BandExperiencePlan(project_id="p", arc_id="arc", band_id=schedule.band_id,
                                       chapter_start=1, chapter_end=8,
                                       schedule_json=schedule.model_dump_json()))
    yield Session, schedule
    engine.dispose()


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("path", ["normal", "repair", "override"])
@pytest.mark.parametrize("chapter", [1, 8])
def test_experience_revelations_reach_all_builders_only_at_their_window(experience_book, builder, path, chapter):
    Session, schedule = experience_book
    if path == "override":
        override_band_experience("p", schedule.band_id, BandExperienceOverrideRequest(),
                                 get_session=Session, pipeline=object())
    else:
        with Session.begin() as session:
            repo = StateRepository(session)
            if path == "repair":
                RepairPlanningStage()._replace_band_schedule(
                    session=session, repo=repo, project_id="p", chapter_number=1, schedule=schedule,
                )
            else:
                for number in (1, 8):
                    plan = repo.get_chapter_plan("p", number)
                    overlay = ChapterExperiencePlanner().derive_chapter_experience_plan(
                        chapter_number=number, experience=repo.get_beat_sheet_experience("p"),
                        schedule=schedule, chapter_plan=plan,
                    )
                    plan.experience_plan_json = overlay.model_dump_json()
    with Session() as session:
        repo = StateRepository(session)
        draft = ContextDraft()
        ExperienceContextProvider().contribute(
            ContextRequest(project_id="p", chapter_plan=repo.get_chapter_plan("p", chapter), repo=repo), draft,
        )
        context = _context(
            ChapterWorldView(), chapter_number=chapter, reader_promise=draft.data["reader_promise"],
            arc_payoff_map=draft.data["arc_payoff_map"], band_delight_schedule=draft.data["band_schedule"],
            chapter_experience_plan=draft.data["chapter_experience_plan"],
        )
        text = _prompt(builder, context)
        assert (SECRET in text) == (chapter >= 8)
        assert ("HiddenKeeper" in text) == (chapter >= 8)
        assert SAFE_PROMISE in text
        assert "Every inference needs evidence" in text
        assert len(context.arc_payoff_map.revelation_layers) == (1 if chapter >= 8 else 0)
        # Author readers retain the complete future layer after every writer projection.
        assert repo.get_arc_payoff_map("p").revelation_layers[0].summary == SECRET
        assert repo.get_beat_sheet_experience("p").arc_payoff_map.revelation_layers[0].summary == SECRET


@pytest.mark.parametrize(("window", "chapter", "visible"), [
    ("8", 7, False), ("8", 8, True), ("8-10", 7, False), ("8-10", 8, True),
    ([8, 10], 11, True), (" 8 – 10 ", 8, True), ("", 8, False), ("later", 8, False),
    ("10-8", 10, False), ("0", 1, False), ("-1", 1, False),
])
def test_writer_revelation_windows_require_an_unambiguous_start(window, chapter, visible):
    original = ArcPayoffMap(revelation_layers=[RevelationLayer(summary=SECRET, chapter_window=window)])
    projected = writer_payoff_map(original, chapter)
    assert bool(projected.revelation_layers) is visible
    assert original.revelation_layers[0].summary == SECRET
    if visible:
        projected.revelation_layers[0].summary = "changed"
        assert original.revelation_layers[0].summary == SECRET
