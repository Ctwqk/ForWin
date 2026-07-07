from __future__ import annotations

import json

from forwin.book_genesis_core.arc_activation_review import build_arc_activation_review_pack
from forwin.book_genesis_core.planning import _plan_arc_chapters
from forwin.book_state.repository import BookStateRepository
from forwin.models import (
    ArcPlanVersion,
    ChapterDraft,
    ChapterPlan,
    DecisionEvent,
    NarrativeObligationRow,
    Project,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.publisher import SignalWindowAggregate
from forwin.protocol.book_state import FactNode, WorldNode
from tests.postgres import postgres_test_url


def test_arc_activation_review_pack_collects_canon_feedback() -> None:
    engine = get_engine(postgres_test_url("arc_activation_review_pack"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(title="Arc Pack", premise="前提", genre="玄幻")
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=1,
                chapter_end=3,
                arc_synopsis="第一卷",
            )
            session.add(arc)
            session.flush()
            plan = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
                status="accepted",
            )
            session.add(plan)
            session.flush()
            session.add(
                ChapterDraft(
                    chapter_plan_id=plan.id,
                    version=1,
                    body_text="林夜获得玄铁令。",
                    summary="林夜获得玄铁令并进入问心阁。",
                )
            )
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project.id,
                    node_type="character",
                    name="林夜",
                    summary="主角",
                    importance=9,
                    state={"status": "持有玄铁令"},
                )
            )
            repo.create_fact_node(
                FactNode(
                    id="fact_token",
                    project_id=project.id,
                    proposition="林夜持有玄铁令。",
                    fact_type="possession",
                    source_refs=["chapter:1"],
                    created_at_chapter=1,
                )
            )
            session.add(
                NarrativeObligationRow(
                    project_id=project.id,
                    origin_chapter_number=1,
                    obligation_type="reader_promise_payoff",
                    priority="P0",
                    status="active",
                    summary="三章内解释玄铁令代价。",
                    deadline_chapter=3,
                )
            )
            session.add(
                DecisionEvent(
                    project_id=project.id,
                    chapter_number=1,
                    event_family="director_imbalance",
                    event_type="director_imbalance_detected",
                    summary="连续两章缺少兑现。",
                    payload_json=json.dumps({"signal": "low_payoff"}, ensure_ascii=False),
                )
            )
            session.add(
                SignalWindowAggregate(
                    project_id=project.id,
                    signal_key="pacing:overall",
                    signal_type="pacing",
                    target_name="整体",
                    signal_level="confirmed",
                    window_type="long",
                    window_chapter_start=1,
                    window_chapter_end=1,
                )
            )
            project_id = project.id

        with Session() as session:
            pack = build_arc_activation_review_pack(
                session,
                project_id=project_id,
                arc_number=2,
                chapter_start=4,
            )

        payload = pack.to_prompt_payload()
        assert payload["accepted_chapter_summaries"][0]["summary"] == "林夜获得玄铁令并进入问心阁。"
        assert payload["important_character_state"][0]["name"] == "林夜"
        assert payload["book_state_facts"][0]["proposition"] == "林夜持有玄铁令。"
        assert payload["open_obligations"][0]["priority"] == "P0"
        assert payload["recent_director_imbalance"][0]["summary"] == "连续两章缺少兑现。"
        assert payload["audience_signals"][0]["signal_type"] == "pacing"
    finally:
        engine.dispose()


def test_plan_arc_chapters_injects_arc_activation_review_pack() -> None:
    class Owner:
        def __init__(self) -> None:
            self.messages = []
            self.max_tokens = 0

        def _call_json_with_trace(self, *, messages, fallback, stage_key, max_tokens):  # noqa: ANN001
            self.messages = messages
            self.max_tokens = max_tokens
            return {
                "chapters": [
                    {
                        "title": "第二卷开局",
                        "one_line": "围绕玄铁令代价开局。",
                        "goals": ["回收玄铁令代价"],
                    }
                ]
            }, {"input_snapshot": {}}

    owner = Owner()
    planned, trace = _plan_arc_chapters(
        owner,
        project=Project(id="project-1", title="测试", premise="前提", genre="玄幻"),
        pack={"book_brief": {"title": "测试"}, "world": {}},
        arc_payload={"arc_number": 2, "arc_synopsis": "第二卷"},
        chapter_count=1,
        arc_activation_review_pack={
            "accepted_chapter_summaries": [{"chapter_number": 1, "summary": "林夜获得玄铁令。"}],
            "open_obligations": [{"priority": "P0", "summary": "解释玄铁令代价"}],
        },
    )

    prompt = owner.messages[-1]["content"]
    assert "ArcActivationReviewPack" in prompt
    assert "林夜获得玄铁令" in prompt
    assert "解释玄铁令代价" in prompt
    assert owner.max_tokens >= 1800
    assert trace["input_snapshot"]["arc_activation_review_pack"]["open_obligations"][0]["priority"] == "P0"
    assert planned[0]["title"] == "第二卷开局"


def test_plan_arc_chapters_rebases_generic_numeric_titles_to_absolute_chapters() -> None:
    class Owner:
        max_tokens = 2000

        def _call_json_with_trace(self, *, messages, fallback, stage_key, max_tokens):  # noqa: ANN001
            return {
                "chapters": [
                    {"title": "第1章", "one_line": "重启追踪。", "goals": ["推进"]},
                    {"title": "第2章：潮门", "one_line": "抵达潮门。", "goals": ["抵达"]},
                    {"title": "债权人的回声", "one_line": "揭示新债权人。", "goals": ["揭示"]},
                ]
            }, {"input_snapshot": {}}

    planned, _trace = _plan_arc_chapters(
        Owner(),
        project=Project(id="project-1", title="测试", premise="前提", genre="玄幻"),
        pack={"book_brief": {"title": "测试"}, "world": {}},
        arc_payload={
            "arc_number": 3,
            "arc_synopsis": "第三卷",
            "chapter_start": 28,
            "chapter_end": 30,
            "chapter_count": 3,
        },
        chapter_count=3,
        arc_activation_review_pack={},
    )

    assert [item["title"] for item in planned] == ["第28章", "第29章：潮门", "债权人的回声"]


def test_plan_arc_chapters_marks_deterministic_fallback_as_degraded() -> None:
    class Owner:
        max_tokens = 2000

        def _call_json_with_trace(self, *, messages, fallback, stage_key, max_tokens):  # noqa: ANN001
            return fallback, {"output_summary": {"mode": "fallback"}, "input_snapshot": {}}

    planned, trace = _plan_arc_chapters(
        Owner(),
        project=Project(id="project-1", title="测试", premise="前提", genre="玄幻"),
        pack={"book_brief": {"title": "测试"}, "world": {}},
        arc_payload={"arc_number": 2, "arc_synopsis": "第二卷"},
        chapter_count=1,
        arc_activation_review_pack={},
    )

    assert trace["arc_planning_status"] == "degraded"
    assert trace["output_summary"]["planning_status"] == "degraded"
    assert planned[0]["planning_status"] == "degraded"
