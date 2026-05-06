from __future__ import annotations

from forwin.experience.arc_experience_planner import ArcExperiencePlanningService
from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.service import AudienceCalibrationProfile
from forwin.models.project import ChapterPlan, Project
from forwin.planning.arc_structure_service import ArcStructureDraftData


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

    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:3",
        chapter_start=1,
        chapter_end=3,
        structure=structure,
        arc_experience=arc_experience,
        active_band=chapters,
        calibration=AudienceCalibrationProfile(clarify_rule_legibility=True),
    )
    chapter_overlay = ChapterExperiencePlanner().derive_chapter_experience_plan(
        chapter_number=2,
        structure=structure,
        arc_experience=arc_experience,
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
