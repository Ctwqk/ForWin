from __future__ import annotations

from forwin.context.assembler import _build_canon_quality_context
from forwin.llm_eval.cases import sample_context
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.reviewer.context_builder import build_review_context_pack
from forwin.writer.prompts import _canon_quality_context_section


def test_canon_quality_context_injects_active_narrative_obligations() -> None:
    engine = get_engine(postgres_test_url("obligation_context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="上下文义务", premise="测试", genre="悬疑", target_total_chapters=12)
            session.add(project)
            session.flush()
            repo = NarrativeObligationRepository(session)
            repo.create_obligation(
                NarrativeObligation(
                    id="obl-context",
                    project_id=project.id,
                    origin_chapter_number=10,
                    obligation_type="motivation_gap",
                    priority="P1",
                    status="active",
                    summary="韩砚动机尚未解释。",
                    hardness="design_debt",
                    deadline_chapter=11,
                    payoff_test="第11章必须给出韩砚动机证据。",
                    linked_plan_patch_ids=["patch-context"],
                )
            )
            session.commit()

        with session_factory() as session:
            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=11,
                target_total_chapters=12,
                chapter_title="第十一章",
                chapter_summary="",
            )

        obligations = context["active_narrative_obligations"]
        assert len(obligations) == 1
        assert obligations[0]["id"] == "obl-context"
        assert obligations[0]["must_resolve_now"] is True
        assert obligations[0]["payoff_test"] == "第11章必须给出韩砚动机证据。"
    finally:
        engine.dispose()


def test_writer_prompt_section_includes_active_narrative_obligations() -> None:
    context = sample_context()
    context.canon_quality_context = {
        "active_narrative_obligations": [
            {
                "id": "obl-context",
                "type": "motivation_gap",
                "priority": "P1",
                "summary": "韩砚动机尚未解释。",
                "deadline_chapter": context.chapter_number,
                "payoff_test": "本章必须给出明确动机证据。",
                "must_resolve_now": True,
            }
        ]
    }

    section = _canon_quality_context_section(context)

    assert section is not None
    assert "叙事义务" in section
    assert "obl-context" in section
    assert "本章必须给出明确动机证据" in section


def test_reviewer_context_pack_carries_active_narrative_obligations() -> None:
    context = sample_context()
    obligation = {
        "id": "obl-review",
        "type": "motivation_gap",
        "priority": "P1",
        "summary": "韩砚动机尚未解释。",
        "deadline_chapter": context.chapter_number,
        "payoff_test": "本章必须给出明确动机证据。",
        "must_resolve_now": True,
    }
    context.canon_quality_context = {"active_narrative_obligations": [obligation]}

    review_context = build_review_context_pack(context=context)

    assert review_context.active_narrative_obligations == [obligation]
