from __future__ import annotations

import json
from types import SimpleNamespace

from forwin.genesis.handoff.chapter_materializer import GenesisChapterMaterializer
from forwin.genesis.planning import _plan_arc_chapters
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


class _StubArcPlanningOwner:
    """Owner exposing the real arc planner behind a stub LLM call."""

    max_tokens = 2000
    _plan_arc_chapters = _plan_arc_chapters

    def __init__(self, pack: dict) -> None:
        self._pack = pack
        self.llm_calls: list[dict] = []

    def load_pack(self, _revision) -> dict:
        return self._pack

    def _call_json_with_trace(self, *, messages, fallback, stage_key, max_tokens):  # noqa: ANN001
        self.llm_calls.append({"messages": messages, "stage_key": stage_key})
        user_prompt = messages[-1]["content"]
        # The prompt declares the absolute chapter range; echo one chapter per slot.
        chapter_count = len(fallback["chapters"])
        return {
            "chapters": [
                {
                    "title": f"stub-title-{index}",
                    "one_line": f"stub-line-{index}",
                    "goals": [f"stub-goal-{index}"],
                }
                for index in range(1, chapter_count + 1)
            ]
        }, {"input_snapshot": {}, "prompt_seen": user_prompt}


def test_materializer_synthesizes_payload_for_arc_missing_from_blueprint() -> None:
    engine = get_engine(postgres_test_url("genesis_arc_materialization"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session() as session:
            updater = StateUpdater(session)
            project = Project(title="Arc Extend", premise="前提", genre="玄幻")
            session.add(project)
            session.flush()
            arc_one = updater.create_arc_plan(
                project_id=project.id,
                arc_synopsis="蓝图里的第一卷",
                status="completed",
                arc_number=1,
                chapter_start=1,
                chapter_end=2,
                planned_target_size=2,
            )
            for number in (1, 2):
                plan = updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc_one.id,
                    chapter_number=number,
                    title=f"第{number}章",
                    one_line="已完成",
                    goals=["已完成"],
                )
                plan.status = "accepted"
            # Arc 2 is what extend-generation appends: absent from the Genesis blueprint.
            arc_two = updater.create_arc_plan(
                project_id=project.id,
                arc_synopsis="续写弧线：第3-5章\n用户给出的续写方向",
                status="active",
                arc_number=2,
                chapter_start=3,
                chapter_end=5,
                planned_target_size=3,
            )
            session.flush()

            pack = {
                "book_arc_blueprint": {
                    "arcs": [
                        {
                            "arc_number": 1,
                            "title": "第一卷",
                            "arc_synopsis": "蓝图里的第一卷",
                            "chapter_start": 1,
                            "chapter_end": 2,
                            "chapter_count": 2,
                        }
                    ]
                }
            }
            owner = _StubArcPlanningOwner(pack)

            row = GenesisChapterMaterializer(owner).materialize_arc_chapter_plans(
                session=session,
                updater=updater,
                project=project,
                revision=SimpleNamespace(id="rev-stub"),
                arc_number=2,
                ensure_arc_map=False,
            )
            session.flush()

            assert row.id == arc_two.id
            plans = (
                session.query(ChapterPlan)
                .filter(ChapterPlan.arc_plan_id == arc_two.id)
                .order_by(ChapterPlan.chapter_number)
                .all()
            )
            assert [plan.chapter_number for plan in plans] == [3, 4, 5]
            assert [plan.title for plan in plans] == [
                "stub-title-1",
                "stub-title-2",
                "stub-title-3",
            ]
            assert [plan.status for plan in plans] == ["planned"] * 3

            assert len(owner.llm_calls) == 1
            assert owner.llm_calls[0]["stage_key"] == "launch_arc_2"
            prompt = owner.llm_calls[0]["messages"][-1]["content"]
            assert "续写弧线：第3-5章" in prompt
            assert "用户给出的续写方向" in prompt
            arc_line = next(
                line for line in prompt.splitlines() if line.startswith("当前 Arc：")
            )
            arc_payload = json.loads(arc_line.removeprefix("当前 Arc："))
            assert arc_payload == {
                "arc_number": 2,
                "title": "",
                "arc_synopsis": "续写弧线：第3-5章\n用户给出的续写方向",
                "goal": "",
                "stakes": "",
                "payoff_direction": "",
                "chapter_start": 3,
                "chapter_end": 5,
                "chapter_count": 3,
                "target_size": 3,
            }
    finally:
        engine.dispose()
