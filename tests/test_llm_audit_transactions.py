from functools import partial
import json

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from forwin.canon.admission import CanonAdmissionService  # noqa: F401
from forwin.maintenance.post_canon import build_world_tick_service, save_world_prompt_trace
from forwin.models.base import Base
from forwin.models.project import Project
from forwin.models.genesis import PromptTrace
from forwin.models.audit import DecisionEvent
from forwin.naming.entity_registrar import LLMEntityAdmissionClassifier
from forwin.protocol.writer import WriterOutput
from tests.test_arc_beat_sheet import StubLLM


@pytest.fixture(params=["sqlite", "postgresql"])
def audit_sessions(request, tmp_path):
    if request.param == "postgresql":
        from tests.postgres import postgres_test_url
        engine = create_engine(postgres_test_url("independent-llm-audit"))
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Project(id="audit-project", title="Audit", premise="Audit", genre="pulp"))
    yield factory
    engine.dispose()


def _trace(stage, attempts=None):
    return {"stage_key": stage, "input_snapshot": {"project_id": "audit-project", "chapter_number": 1},
            "attempts": attempts or [{"stage_key": stage, "output_chars": 10}]}


@pytest.mark.parametrize("stage", ["world.actor_turn", "world.adjudicate", "world.drift"])
def test_world_trace_survives_step_rollback(audit_sessions, stage):
    service = build_world_tick_service(llm_client=StubLLM(), session_factory=audit_sessions, artifact_store=None)
    with audit_sessions() as session:
        session.get(Project, "audit-project").title = "Uncommitted change"
        service.save_prompt_trace(session, _trace(stage))
        session.rollback()
    with audit_sessions() as session:
        trace = session.scalars(select(PromptTrace)).one()
        event = session.scalars(select(DecisionEvent)).one()
        assert trace.stage_key == stage
        assert session.get(Project, "audit-project").title == "Audit"
        assert event.related_object_id == trace.id


@pytest.mark.parametrize("response", ["success", "repair", "provider_failure"])
def test_classifier_owns_attempts_and_survives_rollback(audit_sessions, response):
    answer = {"decisions": [{"name": "Person", "decision": "background_generic"}]}
    replies = [answer] if response == "success" else (["{}", answer] if response == "repair" else [RuntimeError("down")])
    client = StubLLM(*replies)
    with audit_sessions() as session:
        classifier = LLMEntityAdmissionClassifier(client, save_trace=partial(
            save_world_prompt_trace, session, independent=True,
        ))
        kwargs = dict(project_id="audit-project", chapter_number=1, names=["Person"], existing_entities=[],
                      writer_output=WriterOutput(project_id="audit-project", chapter_number=1, title="Title", body="Person", end_of_chapter_summary="Seen"))
        if response == "provider_failure":
            with pytest.raises(RuntimeError, match="down"):
                classifier.classify(**kwargs)
        else:
            assert classifier.classify(**kwargs) == answer["decisions"]
        assert client.drain_llm_attempt_events() == []
        save_world_prompt_trace(session, _trace("next.node"), independent=True)
        session.rollback()
    with audit_sessions() as session:
        traces = list(session.scalars(select(PromptTrace)))
        own = [trace for trace in traces if trace.stage_key.startswith("entity_registrar")]
        assert len(own) == len(client.calls)
        assert all(json.loads(trace.attempts_json)[0]["stage_key"] == trace.stage_key for trace in own)
        following = next(trace for trace in traces if trace.stage_key == "next.node")
        assert [attempt["stage_key"] for attempt in json.loads(following.attempts_json)] == ["next.node"]
        events = list(session.scalars(select(DecisionEvent)))
        assert {trace.id for trace in traces} <= {event.related_object_id for event in events}


@pytest.mark.parametrize("failure", ["runtime", "transaction"])
def test_band_audit_error_survives_rollback(audit_sessions, failure, caplog):
    from tests.test_band_planner import _book, _band, _planner, _plan_band

    class RaisingAudit:
        def audit_plans(self, session, **kwargs):
            if failure == "transaction":
                session.execute(text("SELECT * FROM missing_audit_table"))
            raise RuntimeError("audit broke")

    world, arc_id, sheet_id = _book(audit_sessions)
    client = StubLLM(_band(world, range(1, 5)))
    from forwin.planning.band_planner.types import BandPlanningAuditError
    with pytest.raises(BandPlanningAuditError) as raised:
        _plan_band(audit_sessions, _planner(client, RaisingAudit()), world, arc_id, sheet_id)
    if failure == "transaction":
        assert isinstance(raised.value.__cause__, DBAPIError)
        assert "could not save the failed band audit trace" in caplog.text
    with audit_sessions() as session:
        trace = session.scalars(select(PromptTrace).where(PromptTrace.stage_key == "plan.band")).one()
        assert json.loads(trace.output_summary_json)["failure_reason"] == "audit_error"
        assert session.scalars(select(DecisionEvent).where(DecisionEvent.related_object_id == trace.id)).all()


def test_review_empty_extraction_trace_keeps_candidate_usable(audit_sessions, monkeypatch):
    from forwin.generation.pipeline_core.review_autofix import ReviewWorkflowStage
    from tests.chapter_delta_helpers import stub_chapter_delta_extraction

    stub_chapter_delta_extraction(monkeypatch)
    output = WriterOutput(project_id="audit-project", chapter_number=1, title="Title", body="Body",
                          end_of_chapter_summary="Summary")
    stage = ReviewWorkflowStage()
    stage.llm_client = StubLLM()
    with audit_sessions() as session:
        planned = stage._plan_writer_output_entities(
            session=session, project_id="audit-project", chapter_number=1, writer_output=output,
        )
        assert planned.chapter_delta is not None
        assert planned.body == output.body
        assert not list(session.scalars(select(PromptTrace)))
        assert not list(session.scalars(select(DecisionEvent)))


def test_review_extraction_trace_survives_rollback(audit_sessions, monkeypatch):
    from types import SimpleNamespace
    from forwin.generation.pipeline_core import review_autofix

    output = WriterOutput(project_id="audit-project", chapter_number=1, title="Title", body="Body",
                          end_of_chapter_summary="Summary")
    monkeypatch.setattr(review_autofix, "ensure_chapter_delta", lambda *args, **kwargs: (
        output, SimpleNamespace(prompt_trace=_trace("chapter_delta_extraction")),
    ))
    stage = review_autofix.ReviewWorkflowStage()
    with audit_sessions() as session:
        stage._plan_writer_output_entities(session=session, project_id="audit-project", chapter_number=1,
                                           writer_output=output)
        session.rollback()
    with audit_sessions() as session:
        trace = session.scalars(select(PromptTrace)).one()
        assert trace.stage_key == "chapter_delta_extraction"
        assert session.scalars(select(DecisionEvent).where(DecisionEvent.related_object_id == trace.id)).all()
