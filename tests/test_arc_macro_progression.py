from forwin.models.project import ArcPlanVersion
from forwin.planning.macro_progression import (
    ArcMacroProgression,
    dump_arc_macro_progression,
    load_arc_macro_progression,
)


def test_arc_plan_version_has_macro_progression_json_default() -> None:
    arc = ArcPlanVersion(
        project_id="project-1",
        arc_synopsis="主角从村镇进入县城市场。",
    )

    assert arc.macro_progression_json == "{}"


def test_arc_macro_progression_normalizes_tiers_and_lists() -> None:
    progression = ArcMacroProgression.model_validate(
        {
            "status_promise": "公开赢下县城资格",
            "status_tier_from": "1",
            "status_tier_to": "2",
            "wealth_tier_from": None,
            "wealth_tier_to": 3,
            "enemy_tier_from": 1,
            "enemy_tier_to": "4",
            "market_space_from": "村镇",
            "market_space_to": "县城",
            "ladder_rung_target": "village_to_county",
            "required_boundary_evidence": ["县城资格到手", ""],
            "forbidden_repetition_patterns": ["重复退婚打脸", ""],
        }
    )

    assert progression.status_tier_from == 1
    assert progression.status_tier_to == 2
    assert progression.wealth_tier_from == 0
    assert progression.wealth_tier_to == 3
    assert progression.enemy_tier_to == 4
    assert progression.required_boundary_evidence == ["县城资格到手"]
    assert progression.forbidden_repetition_patterns == ["重复退婚打脸"]


def test_arc_macro_progression_load_dump_round_trip() -> None:
    arc = ArcPlanVersion(project_id="project-1", arc_synopsis="a")
    progression = ArcMacroProgression(
        status_promise="进入内门",
        status_tier_from=1,
        status_tier_to=2,
        market_space_from="外门",
        market_space_to="内门",
        ladder_rung_target="outer_to_inner_sect",
    )

    arc.macro_progression_json = dump_arc_macro_progression(progression)

    assert load_arc_macro_progression(arc).status_promise == "进入内门"
    assert load_arc_macro_progression(arc).status_tier_to == 2
