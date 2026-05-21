from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.service import AudienceCalibrationProfile, ExperiencePlanningService
from forwin.experience.types import ArcExperienceBundle
from forwin.models import Base, SignalWindowAggregate
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise
from forwin.protocol.trope_library import load_trope_template_library


PULP_LIBRARY_PATH = "Design-docs/trope_library_pulp_v1.md"


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _structure() -> ArcStructureDraftData:
    return ArcStructureDraftData(
        phase_layout=["setup", "pressure", "payoff"],
        key_beats=["开局承压", "确认代价", "阶段兑现"],
        thread_priorities=[],
        hotspot_candidates=[],
        compression_candidates=[],
    )


def _arc_experience() -> ArcExperienceBundle:
    return ArcExperienceBundle(
        reader_promise=ReaderPromise(genre_promise="都市", core_pleasures=["打脸"]),
        arc_payoff_map=ArcPayoffMap(),
    )


def test_scheduler_filters_progression_rule_blocked_template_ids(monkeypatch) -> None:
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", PULP_LIBRARY_PATH)
    load_trope_template_library.cache_clear()
    scheduler = BandExperienceScheduler()
    calibration = AudienceCalibrationProfile(
        progression_blocked_template_ids=["power-level-up"],
        progression_blocked_categories=[],
    )

    schedule = scheduler.derive_band_delight_schedule(
        band_id="b1",
        chapter_start=1,
        chapter_end=4,
        structure=_structure(),
        arc_experience=_arc_experience(),
        active_band=[],
        calibration=calibration,
        cost_ceiling=3,
    )

    selected = {item.template_id for item in schedule.scheduled_rewards}
    assert "power-level-up" not in selected
    load_trope_template_library.cache_clear()


def test_feedback_calibration_sets_visible_payoff_for_pacing_signal() -> None:
    session = _session()
    session.add(
        SignalWindowAggregate(
            project_id="p1",
            signal_key="pacing:slow_setup",
            signal_type="pacing",
            target_name="整体",
            window_type="long",
            window_chapter_start=1,
            window_chapter_end=20,
            hit_comment_count=5,
            unique_user_count=4,
            total_comment_count=8,
            reader_estimate=200,
            max_severity=3,
            avg_confidence=0.8,
            signal_level="confirmed",
        )
    )
    session.commit()

    profile = ExperiencePlanningService().build_audience_calibration_profile(
        session=session,
        project_id="p1",
    )

    assert profile.boost_reward_density is True
    assert profile.favor_visible_payoff is True
    assert profile.reduce_setup_ratio is True
