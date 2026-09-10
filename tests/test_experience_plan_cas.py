import pytest
from sqlalchemy.orm import sessionmaker

from forwin.candidate_drafts import candidate_plan_revision
from forwin.models.base import get_engine, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.planning.feedback_plan import FeedbackPlanService
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url
from tests.test_feedback_plan_service import _selected


def test_existing_experience_writer_cannot_overwrite_new_feedback_from_stale_plan():
    engine = get_engine(postgres_test_url("feedback-plan-independent-review"))
    init_db(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(
            Project(id="book", title="Book", premise="Root", target_total_chapters=10)
        )
        session.flush()
        session.add(ArcPlanVersion(id="arc", project_id="book", arc_synopsis="Arc"))
        session.flush()
        session.add(
            ChapterPlan(
                id="book-3",
                project_id="book",
                arc_plan_id="arc",
                chapter_number=3,
                experience_plan_json=ChapterExperiencePlan(
                    rule_anchors=["Existing rule"]
                ).model_dump_json(),
            )
        )
        session.commit()
        _selected(session)
    with factory() as prior_writer:
        before = prior_writer.get(ChapterPlan, "book-3")
        expected = candidate_plan_revision(before)
        planned = ChapterExperiencePlan.model_validate_json(before.experience_plan_json)
        # A normal plan writer captured its typed input before feedback was applied.
        planned.progress_markers.append("Other owner adds progress")
        with factory() as feedback:
            plan = feedback.get(ChapterPlan, "book-3")
            result = FeedbackPlanService().apply(
                session=feedback,
                project_id="book",
                action_id="action",
                chapter_number=3,
                expected_plan_revision=candidate_plan_revision(plan),
            )
            assert result.status == "applied"
            feedback.commit()
        with pytest.raises(ValueError, match="plan revision changed"):
            StateUpdater(prior_writer).update_chapter_experience_plan(
                "book", 3, planned, expected_plan_revision=expected
            )
        prior_writer.rollback()
    with factory() as observer:
        current = ChapterExperiencePlan.model_validate_json(
            observer.get(ChapterPlan, "book-3").experience_plan_json
        )
        assert "Clarify the existing rule's cost." in current.rule_anchors
    engine.dispose()


def test_updater_preserves_unflushed_caller_plan_changes():
    engine = get_engine(postgres_test_url("feedback-plan-dirty-review"))
    init_db(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(Project(id="book", title="Book", premise="Root"))
        session.flush()
        session.add(ArcPlanVersion(id="arc", project_id="book", arc_synopsis="Arc"))
        session.flush()
        plan = ChapterPlan(
            id="book-3", project_id="book", arc_plan_id="arc", chapter_number=3
        )
        session.add(plan)
        session.commit()
        expected = candidate_plan_revision(plan)
        plan.one_line = "Unflushed caller edit"
        with pytest.raises(ValueError, match="unflushed"):
            StateUpdater(session).update_chapter_experience_plan(
                "book", 3, ChapterExperiencePlan(), expected_plan_revision=expected
            )
        assert plan.one_line == "Unflushed caller edit"
        assert plan in session.dirty
    engine.dispose()


def test_band_conflict_after_payload_generation_rolls_back_every_band_write():
    from sqlalchemy import select

    from forwin.experience.service import ExperiencePlanningService
    from forwin.experience.types import ArcExperienceBundle
    from forwin.models.phase import BandExperiencePlan
    from forwin.planning.arc_structure_service import ArcStructureDraftData
    from forwin.planning.band_plan_service import BandPlanningRequest, BandPlanService
    from forwin.protocol import ArcPayoffMap, ReaderPromise
    from tests.test_band_plan_service import _SubworldManager, _WorldContracts

    engine = get_engine(postgres_test_url("feedback-band-cas-review"))
    init_db(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(
            Project(id="book", title="Book", premise="Root", target_total_chapters=10)
        )
        session.flush()
        session.add(ArcPlanVersion(id="arc", project_id="book", arc_synopsis="Arc"))
        session.flush()
        session.add_all(
            [
                ChapterPlan(
                    id=f"book-{n}",
                    project_id="book",
                    arc_plan_id="arc",
                    chapter_number=n,
                    experience_plan_json=ChapterExperiencePlan(
                        rule_anchors=["Original"]
                    ).model_dump_json(),
                )
                for n in (1, 2, 3)
            ]
        )
        session.commit()
        _selected(session)

    class ConcurrentFeedback(ExperiencePlanningService):
        def build_audience_calibration_profile(self, **kwargs):
            with factory() as feedback:
                plan = feedback.get(ChapterPlan, "book-3")
                result = FeedbackPlanService().apply(
                    session=feedback,
                    project_id="book",
                    action_id="action",
                    chapter_number=3,
                    expected_plan_revision=candidate_plan_revision(plan),
                )
                assert result.status == "applied"
                feedback.commit()
            return super().build_audience_calibration_profile(**kwargs)

    with factory() as session:
        plans = session.scalars(
            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
        ).all()
        before = {p.id: p.experience_plan_json for p in plans}
        owner = BandPlanService(
            experience_service=ConcurrentFeedback(),
            subworld_manager=_SubworldManager(),
            world_contract_service=_WorldContracts(),
        )
        with pytest.raises(ValueError, match="plan revision changed"):
            owner.ensure_current_band_plan(
                session=session,
                request=BandPlanningRequest(
                    project_id="book",
                    arc_id="arc",
                    activation_chapter=1,
                    detailed_band_size=3,
                    chapter_plans=plans,
                    structure=ArcStructureDraftData(
                        phase_layout=["setup", "pressure", "payoff"],
                        key_beats=[],
                        thread_priorities=[],
                        hotspot_candidates=[],
                        compression_candidates=[],
                    ),
                    arc_experience=ArcExperienceBundle(
                        reader_promise=ReaderPromise(), arc_payoff_map=ArcPayoffMap()
                    ),
                ),
            )
        # Caller catches the conflict and commits unrelated work: no partial band is left.
        session.commit()
        assert session.scalars(select(BandExperiencePlan)).all() == []
        assert (
            session.get(ChapterPlan, "book-1").experience_plan_json == before["book-1"]
        )
        assert (
            session.get(ChapterPlan, "book-2").experience_plan_json == before["book-2"]
        )
        assert (
            "Clarify the existing rule's cost."
            in session.get(ChapterPlan, "book-3").experience_plan_json
        )
    engine.dispose()


def test_band_failure_after_registry_and_schedule_writes_rolls_back_all():
    from types import SimpleNamespace

    from sqlalchemy import select

    from forwin.experience.types import ArcExperienceBundle
    from forwin.models.phase import BandExperiencePlan
    from forwin.models.subworld import SubWorld
    from forwin.planning.arc_structure_service import ArcStructureDraftData
    from forwin.planning.band_plan_service import BandPlanningRequest, BandPlanService
    from forwin.protocol import ArcPayoffMap, ReaderPromise

    engine = get_engine(postgres_test_url("feedback-band-rollback-review"))
    init_db(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(
            Project(id="book", title="Book", premise="Root", target_total_chapters=10)
        )
        session.flush()
        session.add(ArcPlanVersion(id="arc", project_id="book", arc_synopsis="Arc"))
        session.flush()
        session.add_all(
            [
                ChapterPlan(
                    id=f"book-{n}",
                    project_id="book",
                    arc_plan_id="arc",
                    chapter_number=n,
                )
                for n in (1, 2, 3)
            ]
        )
        session.commit()

    class Activation:
        def plan_band_activation(self, *, session, project_id, **kwargs):
            row = StateUpdater(session).create_subworld(
                project_id=project_id,
                origin_arc_id="arc",
                parent_subworld_id=None,
                name="New region",
                purpose="new stage",
                scope="arc_local",
            )
            return SimpleNamespace(
                active_subworld_ids=[row.id], chapter_entry_targets=[]
            )

    class FailingContracts:
        def ensure_for_arc_band(self, **kwargs):
            session = kwargs["session"]
            assert session.scalars(select(BandExperiencePlan)).all()
            assert session.scalars(select(SubWorld)).all()
            assert session.get(ChapterPlan, "book-1").experience_plan_json != "{}"
            raise RuntimeError("contract evidence unavailable")

    with factory() as session:
        plans = session.scalars(
            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
        ).all()
        owner = BandPlanService(
            subworld_manager=Activation(), world_contract_service=FailingContracts()
        )
        with pytest.raises(RuntimeError, match="contract evidence unavailable"):
            owner.ensure_current_band_plan(
                session=session,
                request=BandPlanningRequest(
                    project_id="book",
                    arc_id="arc",
                    activation_chapter=1,
                    detailed_band_size=3,
                    chapter_plans=plans,
                    structure=ArcStructureDraftData(
                        phase_layout=["setup", "pressure", "payoff"],
                        key_beats=[],
                        thread_priorities=[],
                        hotspot_candidates=[],
                        compression_candidates=[],
                    ),
                    arc_experience=ArcExperienceBundle(
                        reader_promise=ReaderPromise(), arc_payoff_map=ArcPayoffMap()
                    ),
                ),
            )
        session.commit()
        assert session.scalars(select(BandExperiencePlan)).all() == []
        assert session.scalars(select(SubWorld)).all() == []
        assert all(
            p.experience_plan_json == "{}"
            for p in session.scalars(select(ChapterPlan)).all()
        )
    engine.dispose()
