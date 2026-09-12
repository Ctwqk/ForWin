from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.generation.pipeline_core.obligation_resolution import (
    _verify_obligations_after_acceptance,
)
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation
from tests.postgres import postgres_test_url


def _obligation(project_id: str = "project-1", *, payoff_test: str = "第12章必须解释钥匙来源") -> NarrativeObligation:
    return NarrativeObligation(
        project_id=project_id,
        origin_chapter_number=10,
        obligation_type="motivation_gap",
        priority="P1",
        status="active",
        summary="必须解释钥匙来源。",
        hardness="design_debt",
        deadline_chapter=12,
        payoff_test=payoff_test,
        blocking_policy="block_at_deadline",
    )


@pytest.mark.parametrize("chapter_number", [11, 12])
@pytest.mark.parametrize(("kind", "body"), [
    ("motivation_gap", "因为下雨，他在客栈住了一夜。"),
    ("identity_ambiguity", "刺客的身份仍然不明，没有找到证据。"),
    ("custom_reader_promise", "他关灯睡了。"),
])
def test_unreviewed_body_never_resolves_active_obligation(kind, body, chapter_number):
    engine = get_engine(postgres_test_url("unreviewed_resolution"))
    init_db(engine)
    factory = get_session_factory(engine)
    try:
        with factory.begin() as session:
            project = Project(title="义务反例", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            obligation = NarrativeObligationRepository(session).create_obligation(
                _obligation(project.id).model_copy(update={"obligation_type": kind})
            )
            _verify_obligations_after_acceptance(
                SimpleNamespace(policy=SimpleNamespace(review=SimpleNamespace(allows_repair_scope=lambda _: True))),
                session=session, project_id=project.id, chapter_number=chapter_number, accepted_text=body,
            )
            obligation_id = obligation.id
        with factory() as session:
            assert session.get(NarrativeObligationRow, obligation_id).status == ("active" if chapter_number == 11 else "blocked")
    finally:
        engine.dispose()
