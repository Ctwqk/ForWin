from __future__ import annotations

from forwin.experience.arc_experience_planner import ArcExperiencePlanningService
from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.service import AudienceCalibrationProfile
from forwin.experience.types import BeatSheetExperienceInput
from forwin.models.project import ChapterPlan, Project
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.planning.beat_sheet.types import ArcBeatSheetData, BandSpan, BeatPhase, Milestone
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise


def test_experience_services_split_arc_band_and_chapter_planning() -> None:
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
    structure = ArcStructureDraftData(
        phase_layout=["setup", "pressure", "payoff"],
        key_beats=["开场受压", "规则显形", "阶段兑现"],
        thread_priorities=[],
        hotspot_candidates=[],
        compression_candidates=[],
    )
    arc_experience = ArcExperiencePlanningService().plan_arc_experience(
        project=project,
        structure=structure,
        chapter_plans=chapters,
        audience_trends=["confusion:setting:confirmed"],
        drafted_payload={
            "reader_promise": {
                "genre_promise": "玄幻网文",
                "core_pleasures": ["翻盘", "规则"],
                "ambiguity_mode": "stable",
                "world_legibility_target": "规则必须可验证",
            },
            "arc_payoff_map": {"ambiguity_constraints": ["翻盘必须遵守代价"]},
        },
    )

    experience = BeatSheetExperienceInput.from_structure(structure.key_beats, arc_experience)
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

    assert arc_experience.reader_promise.genre_promise == "玄幻网文"
    assert "翻盘必须遵守代价" in arc_experience.arc_payoff_map.ambiguity_constraints
    assert {item.payoff_type for item in schedule.ambiguity_payoffs} == {
        "confirmation",
        "constraint",
        "reversal",
    }
    assert any("规则" in item for item in chapter_overlay.rule_anchors)
    assert "rule" in chapter_overlay.minimum_progress_channels


def test_arc_payoff_ids_from_llm_payload_are_coerced_to_strings() -> None:
    project = Project(id="project-1", title="体验测试", premise="规则雨夜", genre="玄幻")
    structure = ArcStructureDraftData(
        phase_layout=["setup", "pressure", "payoff"],
        key_beats=["开场受压", "规则显形", "阶段兑现"],
        thread_priorities=[],
        hotspot_candidates=[],
        compression_candidates=[],
    )

    arc_experience = ArcExperiencePlanningService().plan_arc_experience(
        project=project,
        structure=structure,
        chapter_plans=[],
        audience_trends=[],
        drafted_payload={
            "arc_payoff_map": {
                "macro_payoffs": [
                    {
                        "payoff_id": 1,
                        "category": "mystery",
                        "target_chapter_hint": 12,
                        "setup_requirement": "关闭主线倒计时",
                        "success_signal": "系统重置被明确解决",
                    }
                ]
            }
        },
    )

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
