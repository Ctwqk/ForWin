from __future__ import annotations

from sqlalchemy import func, select

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.phase import ArcEnvelopeAnalysis, BandExperiencePlan
from forwin.planning.arc_envelope_resolver import ArcEnvelopeResolver
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.state.updater import StateUpdater


def test_arc_envelope_resolver_uses_planned_sizing_without_writing_band_plan() -> None:
    engine = get_engine(postgres_test_url("arc-envelope-resolver"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project("Envelope", "前提", "玄幻")
        project.target_total_chapters = 120
        arc = updater.create_arc_plan(project.id, "计划弧", arc_number=1)
        arc.planned_target_size = 18
        arc.planned_soft_min = 15
        arc.planned_soft_max = 22
        chapters = [
            updater.create_chapter_plan(project.id, arc.id, number, f"第{number}章", f"推进{number}", ["推进"])
            for number in range(1, 9)
        ]

        envelope = ArcEnvelopeResolver().ensure_resolution(
            session=session,
            project=project,
            active_arc=arc,
            chapter_plans=chapters,
            activation_chapter=1,
            structure=ArcStructureDraftData(
                phase_layout=["setup", "payoff"],
                key_beats=["开局", "兑现"],
                thread_priorities=[],
                hotspot_candidates=[],
                compression_candidates=[],
            ),
            rehearsal_report=None,
            preview=None,
        )

        analysis_count = session.scalar(select(func.count()).select_from(ArcEnvelopeAnalysis))
        band_count = session.scalar(select(func.count()).select_from(BandExperiencePlan))

    assert envelope.base_target_size == 18
    assert envelope.base_soft_min == 15
    assert envelope.base_soft_max == 22
    assert envelope.detailed_band_size >= 4
    assert analysis_count == 1
    assert band_count == 0
