from __future__ import annotations

from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.service import AudienceCalibrationProfile
from forwin.experience.types import BeatSheetExperienceInput
from forwin.models.project import ChapterPlan, Project
from forwin.planning.beat_sheet.types import ArcBeatSheetData, BandSpan, BeatPhase, Milestone
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise


def test_experience_services_use_beat_sheet_for_band_and_chapter_planning() -> None:
    project = Project(id="project-1", title="体验测试", premise="规则雨夜", genre="玄幻")
    chapters = [
        ChapterPlan(
            id=f"chapter-{number}",
            project_id=project.id,
            arc_plan_id="arc-1",
            chapter_number=number,
            title=f"第{number}章",
            one_line=f"推进第{number}章",
            goals_json='["推进主线", "明确代价"]',
        )
        for number in range(1, 4)
    ]
    experience = BeatSheetExperienceInput.from_beat_sheet(ArcBeatSheetData(
        protagonist_ref="hero",
        band_breakdown=[BandSpan(band_index=0, chapter_start=1, chapter_end=3)],
        phases=[BeatPhase(name="setup", chapter_range=(1, 3), purpose="规则显形")],
        reader_promise=ReaderPromise(
            genre_promise="玄幻网文", core_pleasures=["翻盘", "规则"],
            ambiguity_mode="stable", world_legibility_target="规则必须可验证",
        ),
        arc_payoff_map=ArcPayoffMap(ambiguity_constraints=["翻盘必须遵守代价"]),
    ))
    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:3",
        chapter_start=1,
        chapter_end=3,
        experience=experience,
        active_band=chapters,
        calibration=AudienceCalibrationProfile(clarify_rule_legibility=True),
    )
    chapter_overlay = ChapterExperiencePlanner().derive_chapter_experience_plan(
        chapter_number=2,
        experience=experience,
        schedule=schedule,
        chapter_plan=chapters[1],
        calibration=AudienceCalibrationProfile(clarify_rule_legibility=True),
    )

    assert experience.reader_promise.genre_promise == "玄幻网文"
    assert "翻盘必须遵守代价" in experience.arc_payoff_map.ambiguity_constraints
    assert {item.payoff_type for item in schedule.ambiguity_payoffs} == {
        "confirmation",
        "constraint",
        "reversal",
    }
    assert any("规则" in item for item in chapter_overlay.rule_anchors)
    assert "rule" in chapter_overlay.minimum_progress_channels


def test_arc_payoff_ids_from_llm_payload_are_coerced_to_strings() -> None:
    data = ArcBeatSheetData.model_validate({
        "protagonist_ref": "hero", "phases": [], "band_breakdown": [],
        "arc_payoff_map": {"macro_payoffs": [{
            "payoff_id": 1, "category": "mystery", "target_chapter_hint": 12,
            "setup_requirement": "关闭主线倒计时", "success_signal": "系统重置被明确解决",
        }]},
    })
    arc_experience = BeatSheetExperienceInput.from_beat_sheet(data)

    payoff = arc_experience.arc_payoff_map.macro_payoffs[0]
    assert payoff.payoff_id == "1"
    assert payoff.target_chapter_hint == "12"


def _beat_sheet() -> ArcBeatSheetData:
    return ArcBeatSheetData(
        protagonist_ref="hero",
        phases=[
            BeatPhase(name="phase-b", chapter_range=(7, 12), purpose="purpose-b"),
            BeatPhase(name="phase-a", chapter_range=(1, 6), purpose="purpose-a"),
        ],
        milestones=[
            Milestone(id="m-late", description="milestone-late", target_range=(10, 12)),
            Milestone(id="m-early", description="milestone-early", target_range=(5, 8)),
        ],
        band_breakdown=[BandSpan(band_index=0, chapter_start=1, chapter_end=6),
                        BandSpan(band_index=1, chapter_start=7, chapter_end=12)],
        reader_promise=ReaderPromise(genre_promise="promise-value"),
        arc_payoff_map=ArcPayoffMap(ambiguity_constraints=["constraint-value"]),
    )


def test_beat_sheet_experience_input_reads_the_band_phases_and_milestones() -> None:
    data = _beat_sheet()

    first = BeatSheetExperienceInput.from_beat_sheet(data, BandSpan(band_index=0, chapter_start=1, chapter_end=6))
    second = BeatSheetExperienceInput.from_beat_sheet(data, BandSpan(band_index=1, chapter_start=7, chapter_end=12))
    whole = BeatSheetExperienceInput.from_beat_sheet(data)

    assert first.key_beats == ["purpose-a", "milestone-early"]
    assert second.key_beats == ["purpose-b", "milestone-early", "milestone-late"]
    assert whole.key_beats == ["purpose-a", "purpose-b", "milestone-early", "milestone-late"]
    assert first.reader_promise.genre_promise == "promise-value"
    assert first.arc_payoff_map.ambiguity_constraints == ["constraint-value"]


def test_scheduler_and_chapter_planner_read_the_beat_sheet_input() -> None:
    data = _beat_sheet()
    band = BandSpan(band_index=0, chapter_start=1, chapter_end=6)
    experience = BeatSheetExperienceInput.from_beat_sheet(data, band)

    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:6", chapter_start=1, chapter_end=6, experience=experience, active_band=[],
    )
    plan = ChapterPlan(project_id="project-1", arc_plan_id="arc-1", chapter_number=6, title="title-6",
                       one_line="one-line-6", goals_json='["goal-6"]')
    overlay = ChapterExperiencePlanner().derive_chapter_experience_plan(
        chapter_number=6, experience=experience, schedule=schedule, chapter_plan=plan,
    )

    # The first two key beats open the band's first curiosity question and escalate it.
    assert (schedule.curiosity_beats[0].question_open, schedule.curiosity_beats[0].escalated_question) == (
        "purpose-a", "milestone-early",
    )
    assert schedule.immersion_anchor_scene_goal.endswith("purpose-a")
    assert "constraint-value" in overlay.rule_anchors
    assert "goal-6" in overlay.progress_markers


def test_persisted_confusion_and_relationship_interest_calibrate_scheduling_and_overlays() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from forwin.experience.service import ExperiencePlanningService
    from forwin.models import Base, SignalWindowAggregate

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    experience = BeatSheetExperienceInput.from_beat_sheet(_beat_sheet())
    chapter = ChapterPlan(project_id="p", arc_plan_id="arc", chapter_number=1, title="title",
                          one_line="Follow the evidence", goals_json='["Find evidence"]')

    def derive(calibration):
        schedule = BandExperienceScheduler().derive_band_delight_schedule(
            band_id="band:1:3", chapter_start=1, chapter_end=3, experience=experience,
            active_band=[chapter], calibration=calibration,
        )
        overlay = ChapterExperiencePlanner().derive_chapter_experience_plan(
            chapter_number=1, experience=experience, schedule=schedule, chapter_plan=chapter,
            calibration=calibration,
        )
        return schedule, overlay

    try:
        with Session(engine) as session:
            session.add(Project(id="p", title="title", premise="premise", genre="test"))
            session.commit()
            service = ExperiencePlanningService()
            baseline = service.build_audience_calibration_profile(session=session, project_id="p")
            assert not baseline.clarify_rule_legibility
            assert not baseline.protect_character_heat
            old_schedule, old_overlay = derive(baseline)
            for signal in ("confusion", "relationship_interest"):
                session.add(SignalWindowAggregate(
                    project_id="p", signal_key=f"{signal}:arc:overall", signal_type=signal,
                    target_type="arc", target_name="", window_type="long",
                    window_chapter_start=1, window_chapter_end=8, hit_comment_count=5,
                    unique_user_count=4, total_comment_count=10, reader_estimate=500,
                    reader_tier=2, max_severity=2, avg_confidence=0.88, signal_level="confirmed",
                ))
            session.commit()
            session.expire_all()
            calibration = service.build_audience_calibration_profile(session=session, project_id="p")
            assert calibration.clarify_rule_legibility
            assert calibration.protect_character_heat
            schedule, overlay = derive(calibration)

            assert len(schedule.curiosity_beats) == len(old_schedule.curiosity_beats) + 1
            assert any(beat.chapter_hint == 2 for beat in schedule.curiosity_beats)
            assert not any(reward.category == "emotion" for reward in old_schedule.scheduled_rewards)
            assert any(reward.category == "emotion" for reward in schedule.scheduled_rewards)
            assert len(overlay.rule_anchors) == len(old_overlay.rule_anchors) + 1
            assert len(overlay.progress_markers) == len(old_overlay.progress_markers) + 1
            assert {"rule", "relationship"}.isdisjoint(old_overlay.minimum_progress_channels)
            assert {"rule", "relationship"} <= set(overlay.minimum_progress_channels)
            assert not old_overlay.relationship_or_status_shift
            assert overlay.relationship_or_status_shift
    finally:
        engine.dispose()
