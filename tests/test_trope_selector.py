from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.service import AudienceCalibrationProfile
from forwin.experience.types import ArcExperienceBundle
from forwin.models.project import ChapterPlan
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.planning.band_plan_service import BandPlanningRequest, BandPlanService
from forwin.protocol.experience import ArcPayoffMap, MacroPayoff, ReaderPromise
from forwin.protocol.trope_library import load_trope_template_library, trope_template_index


PULP_LIBRARY_PATH = "Design-docs/trope_library_pulp_v1.md"


def _structure() -> ArcStructureDraftData:
    return ArcStructureDraftData(
        phase_layout=["setup", "pressure", "payoff"],
        key_beats=["开局承压", "确认代价", "阶段兑现"],
        thread_priorities=[],
        hotspot_candidates=[],
        compression_candidates=[],
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


def _arc_experience(*, macro_payoffs: list[MacroPayoff] | None = None) -> ArcExperienceBundle:
    return ArcExperienceBundle(
        reader_promise=ReaderPromise(genre_promise="玄幻", core_pleasures=["翻盘"]),
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
        structure=_structure(),
        arc_experience=_arc_experience(),
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
        structure=_structure(),
        arc_experience=_arc_experience(
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


def test_band_plan_service_passes_trope_cost_ceiling_to_scheduler() -> None:
    class _Scheduler:
        cost_ceiling: int | None = None

        def derive_band_delight_schedule(self, **kwargs):
            self.cost_ceiling = kwargs["cost_ceiling"]
            return SimpleNamespace(
                active_subworld_ids=[],
                chapter_entry_targets=[],
                model_copy=lambda update: SimpleNamespace(
                    active_subworld_ids=[],
                    chapter_entry_targets=[],
                    model_copy=lambda update: SimpleNamespace(
                        active_subworld_ids=[],
                        chapter_entry_targets=[],
                    ),
                ),
            )

    class _WindowResolver:
        def resolve(self, **_kwargs):
            return SimpleNamespace(
                band_id="band:1:1",
                chapter_start=1,
                chapter_end=1,
                active_band=_chapters()[:1],
            )

    class _ExperienceService:
        def build_audience_calibration_profile(self, **_kwargs):
            return AudienceCalibrationProfile()

    class _SubworldManager:
        def plan_band_activation(self, **_kwargs):
            return SimpleNamespace(active_subworld_ids=[], chapter_entry_targets=[])

    class _ChapterPlanner:
        def derive_chapter_experience_plan(self, **_kwargs):
            return SimpleNamespace(model_copy=lambda update: SimpleNamespace())

    class _Persistence:
        def save_band_experience_plan(self, **_kwargs) -> None:
            return None

        def save_chapter_experience_plan(self, **_kwargs) -> None:
            return None

    class _WorldContractService:
        def ensure_for_arc_band(self, **_kwargs) -> None:
            return None

    class _Session:
        def get(self, *_args, **_kwargs):
            return None

        def add(self, *_args, **_kwargs) -> None:
            return None

        def flush(self) -> None:
            return None

    scheduler = _Scheduler()
    service = BandPlanService(
        subworld_manager=_SubworldManager(),
        world_contract_service=_WorldContractService(),
        experience_service=_ExperienceService(),
        scheduler=scheduler,
        chapter_planner=_ChapterPlanner(),
        persistence=_Persistence(),
        window_resolver=_WindowResolver(),
        trope_cost_ceiling=1,
    )

    service.ensure_current_band_plan(
        session=_Session(),
        request=BandPlanningRequest(
            project_id="project-1",
            arc_id="arc-1",
            activation_chapter=1,
            detailed_band_size=1,
            chapter_plans=_chapters()[:1],
            structure=_structure(),
            arc_experience=_arc_experience(),
        ),
    )

    assert scheduler.cost_ceiling == 1
