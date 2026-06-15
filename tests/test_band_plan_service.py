from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select

from forwin.experience.types import ArcExperienceBundle
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.planning.band_plan_service import BandPlanningRequest, BandPlanService
from forwin.protocol import ArcPayoffMap, ChapterEntryTarget, ReaderPromise
from forwin.state.updater import StateUpdater


class _SubworldManager:
    def plan_band_activation(self, **_kwargs):
        return SimpleNamespace(
            active_subworld_ids=["global-core"],
            chapter_entry_targets=[
                ChapterEntryTarget(
                    chapter_hint=1,
                    entity_name="阿青",
                    subworld_id="global-core",
                    role_hint="常驻核心",
                )
            ],
        )


class _WorldContracts:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_for_arc_band(self, **_kwargs) -> None:
        self.calls += 1


def test_band_plan_service_persists_band_and_chapter_experience_overlay() -> None:
    engine = get_engine(postgres_test_url("band-plan-service"))
    init_db(engine)
    Session = get_session_factory(engine)
    contracts = _WorldContracts()

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project("Band", "前提", "玄幻")
        arc = updater.create_arc_plan(project.id, "当前弧", arc_number=1)
        chapters = [
            updater.create_chapter_plan(project.id, arc.id, number, f"第{number}章", f"推进{number}", ["推进"])
            for number in range(1, 5)
        ]

        result = BandPlanService(
            subworld_manager=_SubworldManager(),
            world_contract_service=contracts,
        ).ensure_current_band_plan(
            session=session,
            request=BandPlanningRequest(
                project_id=project.id,
                arc_id=arc.id,
                activation_chapter=1,
                detailed_band_size=3,
                chapter_plans=chapters,
                structure=ArcStructureDraftData(
                    phase_layout=["setup", "pressure", "payoff"],
                    key_beats=["开局", "压力", "兑现"],
                    thread_priorities=[],
                    hotspot_candidates=[],
                    compression_candidates=[],
                ),
                arc_experience=ArcExperienceBundle(
                    reader_promise=ReaderPromise(genre_promise="玄幻"),
                    arc_payoff_map=ArcPayoffMap(),
                ),
            ),
        )

        band_row = session.execute(select(BandExperiencePlan)).scalar_one()
        chapter_one = session.execute(
            select(ChapterPlan).where(ChapterPlan.project_id == project.id, ChapterPlan.chapter_number == 1)
        ).scalar_one()

    band_payload = json.loads(band_row.schedule_json)
    chapter_payload = json.loads(chapter_one.experience_plan_json)
    assert result.band_id == "band:1:3"
    assert result.updated_chapter_numbers == [1, 2, 3]
    assert band_payload["active_subworld_ids"] == ["global-core"]
    assert chapter_payload["entity_admission_rule"] == "strict_named_character"
    assert chapter_payload["active_subworld_ids"] == ["global-core"]
    assert contracts.calls == 1


def test_band_plan_service_admits_named_entry_target_from_chapter_goal() -> None:
    engine = get_engine(postgres_test_url("band-plan-service-plan-entry-target"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project("Band", "前提", "玄幻")
        arc = updater.create_arc_plan(project.id, "当前弧", arc_number=1)
        chapters = [
            updater.create_chapter_plan(
                project.id,
                arc.id,
                number,
                f"第{number}章",
                "推进主线",
                ["推进主线"],
            )
            for number in range(1, 4)
        ]
        chapters.append(
            updater.create_chapter_plan(
                project.id,
                arc.id,
                4,
                "灯塔控制塔的拒绝访问",
                "引入灰鸦作为潜在信息源/盟友",
                ["引入灰鸦作为潜在信息源/盟友"],
            )
        )

        BandPlanService(
            subworld_manager=_SubworldManager(),
            world_contract_service=_WorldContracts(),
        ).ensure_current_band_plan(
            session=session,
            request=BandPlanningRequest(
                project_id=project.id,
                arc_id=arc.id,
                activation_chapter=4,
                detailed_band_size=3,
                chapter_plans=chapters,
                structure=ArcStructureDraftData(
                    phase_layout=["setup", "pressure", "payoff"],
                    key_beats=["开局", "压力", "兑现"],
                    thread_priorities=[],
                    hotspot_candidates=[],
                    compression_candidates=[],
                ),
                arc_experience=ArcExperienceBundle(
                    reader_promise=ReaderPromise(genre_promise="玄幻"),
                    arc_payoff_map=ArcPayoffMap(),
                ),
            ),
        )
        chapter_four = session.execute(
            select(ChapterPlan).where(ChapterPlan.project_id == project.id, ChapterPlan.chapter_number == 4)
        ).scalar_one()

    chapter_payload = json.loads(chapter_four.experience_plan_json)
    targets = chapter_payload["chapter_entry_targets"]
    assert any(item["entity_name"] == "灰鸦" for item in targets)


def test_band_plan_service_admits_contact_and_record_entry_targets_from_chapter_goal() -> None:
    engine = get_engine(postgres_test_url("band-plan-service-contact-entry-target"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project("Band", "前提", "科幻悬疑")
        arc = updater.create_arc_plan(project.id, "当前弧", arc_number=1)
        chapters = [
            updater.create_chapter_plan(
                project.id,
                arc.id,
                number,
                f"第{number}章",
                "推进主线",
                ["推进主线"],
            )
            for number in range(1, 4)
        ]
        chapters.append(
            updater.create_chapter_plan(
                project.id,
                arc.id,
                4,
                "海底旧站的分割体",
                "与003号分割体接触，查明馆员陈潮白存在双重死亡记录",
                ["与003号分割体接触", "馆员陈潮白存在双重死亡记录"],
            )
        )

        BandPlanService(
            subworld_manager=_SubworldManager(),
            world_contract_service=_WorldContracts(),
        ).ensure_current_band_plan(
            session=session,
            request=BandPlanningRequest(
                project_id=project.id,
                arc_id=arc.id,
                activation_chapter=4,
                detailed_band_size=3,
                chapter_plans=chapters,
                structure=ArcStructureDraftData(
                    phase_layout=["setup", "pressure", "payoff"],
                    key_beats=["开局", "压力", "兑现"],
                    thread_priorities=[],
                    hotspot_candidates=[],
                    compression_candidates=[],
                ),
                arc_experience=ArcExperienceBundle(
                    reader_promise=ReaderPromise(genre_promise="科幻悬疑"),
                    arc_payoff_map=ArcPayoffMap(),
                ),
            ),
        )
        chapter_four = session.execute(
            select(ChapterPlan).where(ChapterPlan.project_id == project.id, ChapterPlan.chapter_number == 4)
        ).scalar_one()

    chapter_payload = json.loads(chapter_four.experience_plan_json)
    target_names = {item["entity_name"] for item in chapter_payload["chapter_entry_targets"]}
    assert "003号分割体" in target_names
    assert "陈潮白" in target_names
    assert "馆员陈潮白" not in target_names
