from __future__ import annotations

import json

from sqlalchemy import select

from forwin.arc_sizing import policy_for_total_chapters
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.phase import ArcStructureDraft
from forwin.planning.arc_structure_service import ArcStructurePlanningService
from forwin.state.updater import StateUpdater


class _Director:
    def draft_arc_structure(self, **_kwargs):
        return {
            "phase_layout": ["setup", "pressure", "payoff"],
            "key_beats": ["开场受压", "规则显形", "阶段兑现"],
            "thread_priorities": [{"name": "主线", "priority": 1, "reason": "核心冲突"}],
            "hotspot_candidates": ["规则显形"],
            "compression_candidates": ["过场压缩"],
            "reader_promise": {"genre_promise": "玄幻网文", "core_pleasures": ["翻盘"]},
            "arc_payoff_map": {
                "macro_payoffs": [
                    {
                        "payoff_id": "p1",
                        "category": "power",
                        "template_id": "power-hidden-edge",
                    }
                ],
                "ambiguity_constraints": ["翻盘必须回指规则"],
            },
        }


def test_arc_structure_service_persists_structure_and_separates_experience_payload() -> None:
    engine = get_engine(postgres_test_url("arc-structure-service"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project("结构测试", "主角在雨夜觉醒规则", "玄幻")
        arc = updater.create_arc_plan(project.id, "开篇弧", arc_number=1)
        chapters = [
            updater.create_chapter_plan(project.id, arc.id, number, f"第{number}章", f"推进{number}", ["推进"])
            for number in range(1, 4)
        ]

        result = ArcStructurePlanningService(director=_Director()).ensure_structure(
            session=session,
            project=project,
            active_arc=arc,
            total_chapters=30,
            policy=policy_for_total_chapters(30),
            base_target_size=10,
            chapter_plans=chapters,
            audience_trends=["pacing:arc:confirmed"],
        )

        row = session.execute(select(ArcStructureDraft)).scalar_one()

    assert result.structure.phase_layout == ["setup", "pressure", "payoff"]
    assert result.structure.key_beats == ["开场受压", "规则显形", "阶段兑现"]
    assert not hasattr(result.structure, "reader_promise")
    assert result.experience_payload["reader_promise"]["genre_promise"] == "玄幻网文"
    assert json.loads(row.phase_layout_json) == ["setup", "pressure", "payoff"]
    assert json.loads(row.reader_promise_json) == {}
