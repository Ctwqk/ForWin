from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.service import AudienceCalibrationProfile
from forwin.experience.trope_selector import TropeSelectionContext, TropeSelector
from forwin.experience.types import BeatSheetExperienceInput
from forwin.models.project import ChapterPlan
from forwin.protocol.experience import ArcPayoffMap, MacroPayoff, ReaderPromise
from forwin.protocol.trope_library import TropeTemplate, load_trope_template_library, trope_template_index


PULP_LIBRARY_PATH = "Design-docs/trope_library_pulp_v1.md"


def _structure() -> BeatSheetExperienceInput:
    return BeatSheetExperienceInput(
        key_beats=["开局承压", "确认代价", "阶段兑现"],
    )


def _chapters() -> list[ChapterPlan]:
    return [
        ChapterPlan(
            id=f"chapter-{number}",
            project_id="project-1",
            arc_plan_id="arc-1",
            chapter_number=number,
            title=f"第{number}章",
            one_line=f"推进第{number}章",
            goals_json='["推进"]',
        )
        for number in range(1, 4)
    ]


def _chapter_band(count: int) -> list[ChapterPlan]:
    return [
        ChapterPlan(
            id=f"chapter-{number}",
            project_id="project-1",
            arc_plan_id="arc-1",
            chapter_number=number,
            title=f"第{number}章",
            one_line=f"推进第{number}章",
            goals_json='["推进"]',
        )
        for number in range(1, count + 1)
    ]


def _arc_experience(*, macro_payoffs: list[MacroPayoff] | None = None) -> BeatSheetExperienceInput:
    return BeatSheetExperienceInput(
        reader_promise=ReaderPromise(genre_promise="玄幻", core_pleasures=["翻盘"]),
        arc_payoff_map=ArcPayoffMap(macro_payoffs=macro_payoffs or []),
    )


def _experience(*, macro_payoffs: list[MacroPayoff] | None = None) -> BeatSheetExperienceInput:
    return BeatSheetExperienceInput(
        key_beats=_structure().key_beats,
        reader_promise=_arc_experience().reader_promise,
        arc_payoff_map=ArcPayoffMap(macro_payoffs=macro_payoffs or []),
    )


@pytest.fixture(autouse=True)
def _clear_trope_cache() -> None:
    load_trope_template_library.cache_clear()
    yield
    load_trope_template_library.cache_clear()


def test_cost_ceiling_selects_low_cost_templates_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", PULP_LIBRARY_PATH)

    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:1",
        chapter_start=1,
        chapter_end=1,
        experience=_experience(),
        active_band=_chapters()[:1],
        calibration=AudienceCalibrationProfile(),
        cost_ceiling=1,
    )

    template_index = trope_template_index()
    assert schedule.scheduled_rewards
    for reward in schedule.scheduled_rewards:
        selected = template_index.get(reward.template_id)
        if selected is not None:
            same_category_under_ceiling = [
                item
                for item in template_index.values()
                if item.category == reward.category and item.cost_weight <= 1
            ]
            if same_category_under_ceiling:
                assert selected.cost_weight <= 1


def test_selector_avoids_duplicate_template_ids_when_unused_templates_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", PULP_LIBRARY_PATH)

    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:3",
        chapter_start=1,
        chapter_end=3,
        experience=_experience(
            macro_payoffs=[
                MacroPayoff(
                    payoff_id="power-payoff",
                    category="power",
                    template_id="power-level-up",
                )
            ]
        ),
        active_band=_chapters(),
        calibration=AudienceCalibrationProfile(),
        cost_ceiling=3,
    )

    template_ids = [item.template_id for item in schedule.scheduled_rewards]
    assert template_ids.count("power-level-up") == 1
    assert len(template_ids) == len(set(template_ids))


def test_selector_avoids_third_sub_trope_use_in_twenty_chapter_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", PULP_LIBRARY_PATH)

    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:21:24",
        chapter_start=21,
        chapter_end=24,
        experience=_experience(
            macro_payoffs=[
                MacroPayoff(
                    payoff_id="power-payoff",
                    category="power",
                    template_id="power-level-up",
                )
            ]
        ),
        active_band=_chapter_band(4),
        calibration=AudienceCalibrationProfile(
            recent_template_ids=[
                "social-face-slap",
                "mystery-new-clue",
                "power-hidden-card",
                "power-level-up",
                "emotion-protect",
                "justice-recover-loss",
                "power-level-up",
            ],
            recent_trope_categories=[],
        ),
        cost_ceiling=3,
    )

    selected = [item.template_id for item in schedule.scheduled_rewards]
    assert "power-level-up" not in selected


def test_independent_trope_selector_filters_cost_fit_and_cooldown() -> None:
    templates = [
        TropeTemplate(
            template_id="wrong-genre",
            display_name="wrong-genre",
            category="power",
            cost_weight=1,
            genre_fit=["都市"],
            audience_fit=["番茄免费阅读"],
            platform_fit=["fanqie"],
        ),
        TropeTemplate(
            template_id="overused-template",
            display_name="overused-template",
            category="power",
            cost_weight=1,
            genre_fit=["玄幻"],
            audience_fit=["番茄免费阅读"],
            platform_fit=["fanqie"],
        ),
        TropeTemplate(
            template_id="too-expensive",
            display_name="too-expensive",
            category="power",
            cost_weight=4,
            genre_fit=["玄幻"],
            audience_fit=["番茄免费阅读"],
            platform_fit=["fanqie"],
        ),
        TropeTemplate(
            template_id="chosen-template",
            display_name="chosen-template",
            category="power",
            cost_weight=1,
            genre_fit=["玄幻"],
            audience_fit=["番茄免费阅读"],
            platform_fit=["fanqie"],
        ),
    ]

    selected = TropeSelector().select_template(
        category_templates=templates,
        library_templates=[],
        context=TropeSelectionContext(
            category="power",
            genre="玄幻",
            audience_fit=["番茄免费阅读"],
            platform="fanqie",
            cost_ceiling=2,
            recent_template_ids=["overused-template", "other", "overused-template"],
        ),
    )

    assert selected is not None
    assert selected.template_id == "chosen-template"


def test_boost_reward_density_does_not_duplicate_mid_power_reward() -> None:
    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:4",
        chapter_start=1,
        chapter_end=4,
        experience=_experience(),
        active_band=_chapter_band(4),
        calibration=AudienceCalibrationProfile(boost_reward_density=True),
        cost_ceiling=3,
    )

    mid_power_rewards = [
        item
        for item in schedule.scheduled_rewards
        if item.chapter_hint == 2
        and item.category == "power"
        and item.intent == "micro_progress_power"
    ]
    assert len(mid_power_rewards) == 1


