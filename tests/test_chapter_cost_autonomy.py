import json

import pytest

from forwin.audit.events import DecisionEventType as E
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.models.audit import DecisionEvent
from sqlalchemy import create_engine
from forwin.models.base import Base, get_engine, get_session_factory, init_db
from forwin.models.genesis import PromptTrace
from forwin.models.project import ArcPlanVersion, Project
from forwin.models.phase import BandExperiencePlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.observability.cost_ledger import CostLedgerService, render_cost_ledger_markdown
from forwin.observability.llm_trace import build_llm_decision_event_payloads
from tests.postgres import postgres_test_url


@pytest.fixture(params=["sqlite", "postgres"])
def session(request):
    engine = create_engine("sqlite:///:memory:") if request.param == "sqlite" else get_engine(postgres_test_url("chapter-cost"))
    if request.param == "sqlite":
        Base.metadata.create_all(engine)
    else:
        init_db(engine)
    with get_session_factory(engine)() as session:
        yield session
    engine.dispose()


def test_chapter_cost_and_autonomy(session):
    project = Project(title="metrics", premise="p", genre="g")
    session.add(project)
    session.flush()
    arc = ArcPlanVersion(project_id=project.id, arc_synopsis="a")
    session.add(arc)
    session.flush()
    session.add(BandExperiencePlan(project_id=project.id, arc_id=arc.id, band_id="band:1:4",
                                   chapter_start=1, chapter_end=4))
    other = Project(title="other", premise="p", genre="g")
    session.add(other)
    session.flush()
    session.add(PromptTrace(project_id=other.id, stage_key="chapter_draft",
                            input_snapshot_json='{"chapter_number": 3}',
                            attempts_json='[{"attempt_no": 1, "total_tokens": 999}]'))
    stages = {"writer": "chapter_draft", "reviewer": "chapter_review",
              "extraction": "chapter_delta_extraction", "world_engine": "world.actor_turn",
              "planner": "plan.band", "codex_delegate": "review.world_conflict"}
    for role, stage in stages.items():
        session.add(PromptTrace(
            id=role, project_id=project.id, stage_key=stage,
            input_snapshot_json=json.dumps({"chapter_number": 3, "band_id": "band:1:4"}),
            attempts_json=json.dumps([{"attempt_no": 1, "task_family": "writer" if role == "extraction" else role,
                                      "prompt_tokens": 10, "completion_tokens": 5}]),
            output_summary_json=json.dumps({"band_payload_trim": {
                "before_chars": 1000, "after_chars": 700, "budget_chars": 800,
                "removed": {"facts": 2, "actors": 1}}} if role == "planner" else {}),
        ))
    def event(kind, payload, chapter=3):
        session.add(DecisionEvent(project_id=project.id, chapter_number=chapter,
                                  band_id="band:1:4", event_type=kind, payload_json=json.dumps(payload)))
    # A terminal event already represented by its trace must not add a second call.
    event(E.LLM_REQUEST_SUCCEEDED, {"prompt_trace_id": "writer", "attempt_no": 1, "total_tokens": 15})
    event(E.LLM_REQUEST_SUCCEEDED, {"stage": "writing_chapter", "attempt_no": 1})
    event(E.LLM_REQUEST_FAILED, {"stage_key": "world.adjudicate", "attempt_no": 1})
    event(E.BAND_CHECKPOINT_HIT, attach_gate_outcome({}, GateOutcome(
        gate_id="band_checkpoint", responsibility_domain="band_integrity", scope="band",
        decision="pause", issue_keys=["clock_unanswered", "clock_unanswered", "milestone_uncovered"])))
    event(E.BAND_CHECKPOINT_CREATED, {"issue_codes": ["ignored"]})
    for chapter in (2, 3, 3):
        event(E.WORLD_TICK_CANDIDATE_DROPPED, {"intent_id": "intent-1", "reason": "prompt_too_large_deferred"}, chapter)
    event(E.WORLD_TICK_CANDIDATE_DROPPED, {"intent_id": "intent-2", "reason": "constraint_violation"})
    for status, decider in [("resolved", "human"), ("resolved", "codex"), ("open", "")]:
        session.add(WorldPlanConflictRow(project_id=project.id, source="world_tick", detected_after_chapter=3,
            story_time=0, violated="protection", impact_level="major", required_decider="human",
            blocks_from_chapter=4, status=status, summary="s",
            decision_option="veto_world" if status == "resolved" else "",
            decision_json=json.dumps({"option": "veto_world", "decided_by": decider} if status == "resolved" else {})))
    session.flush()
    service = CostLedgerService(session)
    report = service.report(project_id=project.id, chapter_number=3)
    chapter = report.chapters[0]
    assert chapter.chapter_number == 3 and chapter.project_id == project.id
    roles = {row.role: row.metrics for row in chapter.roles}
    assert set(roles) == set(stages)
    for role, metrics in roles.items():
        assert metrics.attempts == (2 if role == "world_engine" else 1)
        assert metrics.prompt_tokens == 10 and metrics.completion_tokens == 5
    assert report.totals.attempts == 7
    assert service.report(project_id=project.id, chapter_number=3, band_id="band:1:4").chapters == report.chapters
    assert service.report(project_id=project.id, band_id="absent").chapters == []
    assert chapter.world_conflicts == 3 and chapter.human_decided_conflicts == 1
    assert chapter.band_checkpoint_pauses_by_code == {"clock_unanswered": 1, "milestone_uncovered": 1}
    assert chapter.band_payload_trim_events == 1 and chapter.band_payload_removed_chars == 300
    assert chapter.band_payload_removed_items == {"actors": 1, "facts": 2}
    assert chapter.oversized_intent_deferrals == {"intent-1": 2}
    assert chapter.repeatedly_deferred_intents == {"intent-1": 3}
    assert service.report(project_id=project.id, chapter_number=3).model_dump() == report.model_dump()
    from forwin.mcp.models import CostLedgerReportView
    assert CostLedgerReportView.model_validate(report.model_dump()).model_dump() == report.model_dump()
    assert "codex_delegate" in render_cost_ledger_markdown(report)
    assert "clock_unanswered" in render_cost_ledger_markdown(report)
    assert service.report(project_id=project.id, chapter_number=99).chapters == []
    assert service.report(project_id=project.id, candidate_id="missing").chapters == []


def test_missing_cost_and_autonomy_data(session):
    project = Project(title="empty", premise="p", genre="g")
    session.add(project)
    session.flush()
    session.add(PromptTrace(id="empty-trace", project_id=project.id, stage_key="plan.band", attempts_json="bad",
                            output_summary_json="[]", input_snapshot_json='{"chapter_number": 1}'))
    session.flush()
    report = CostLedgerService(session).report(project_id=project.id)
    assert report.totals.attempts == 0
    assert report.chapters[0].band_payload_trim_events == 0
    assert report.chapters[0].human_decided_conflicts == 0
    session.add(DecisionEvent(project_id=project.id, chapter_number=1, event_type=E.LLM_REQUEST_SUCCEEDED,
                              payload_json=json.dumps({"prompt_trace_id": "empty-trace", "stage_key": "plan.band",
                                                       "attempt_no": 1, "prompt_tokens": 2, "completion_tokens": 3})))
    session.flush()
    recovered = CostLedgerService(session).report(project_id=project.id)
    assert recovered.totals.attempts == 1 and recovered.totals.total_tokens == 5
    assert recovered.chapters[0].roles[0].role == "planner"


@pytest.mark.parametrize("status,option,decision", [
    pytest.param("resolved", "", '{"decided_by":"human","option":"veto_world"}', id="blank-option"),
    pytest.param("resolved", None, '{"decided_by":"human"}', id="missing-option"),
    pytest.param("resolved", "unknown", '{"decided_by":"human"}', id="invalid-option"),
    pytest.param("open", "", '{}', id="human-routed"),
    pytest.param("open", "veto_world", '{"decided_by":"human"}', id="open-human-decision"),
    pytest.param("resolved", "veto_world", '{"decided_by":"auto"}', id="auto-decision"),
    pytest.param("resolved", "veto_world", '{"decided_by":"codex"}', id="codex-decision"),
    pytest.param("resolved", "veto_world", '{}', id="missing-decider"),
    pytest.param("resolved", "veto_world", 'bad', id="malformed-decision"),
])
def test_human_decisions_require_resolved_typed_option(session, status, option, decision):
    project = Project(title="decisions", premise="p", genre="g")
    session.add(project)
    session.flush()
    common = dict(project_id=project.id, source="world_tick", detected_after_chapter=3,
                  story_time=0, violated="protection", impact_level="major", required_decider="human",
                  blocks_from_chapter=4, summary="s")
    session.add(WorldPlanConflictRow(**common, status="resolved", decision_option="veto_world",
                                    decision_json='{"option":"veto_world","decided_by":"human"}'))
    control = {"decision_option": option} if option is not None else {}
    session.add(WorldPlanConflictRow(**common, status=status, decision_json=decision, **control))
    session.flush()

    chapter = CostLedgerService(session).report(project_id=project.id, chapter_number=3).chapters[0]
    assert chapter.world_conflicts == 2
    assert chapter.human_decided_conflicts == 1


@pytest.mark.parametrize("attempts_json", ["[]", "bad"], ids=["empty", "malformed"])
@pytest.mark.parametrize("link", ["payload", "related-object"])
@pytest.mark.parametrize("event_chapter", [3, None], ids=["chapter-present", "chapter-missing"])
def test_recovered_events_inherit_linked_trace_context(session, attempts_json, link, event_chapter):
    project = Project(title="recovery", premise="p", genre="g")
    session.add(project)
    session.flush()
    trace = PromptTrace(
        id="trace", project_id=project.id, stage_key="plan.band", attempts_json=attempts_json,
        input_snapshot_json=json.dumps({"chapter_number": 3, "candidate_id": "candidate-3",
                                        "band_id": "band:1:4", "gate_id": "band_checkpoint"}),
        model_profile_json='{"model":"planner-model","provider":"planner-provider"}',
    )
    session.add(trace)
    attempt = {"attempt_no": 1, "output_chars": 4, "prompt_tokens": 2, "completion_tokens": 3}
    terminal = build_llm_decision_event_payloads(
        {"stage_key": "plan.band", "attempts": [attempt]}, prompt_trace_id=trace.id,
    )[0]
    assert "stage_key" not in terminal["payload"]
    related = {}
    if link == "related-object":
        terminal["payload"].pop("prompt_trace_id")
        related = {"related_object_type": "prompt_trace", "related_object_id": trace.id}
    session.add(DecisionEvent(project_id=project.id, chapter_number=event_chapter,
                              event_type=terminal["event_type"], payload_json=json.dumps(terminal["payload"]),
                              **related))
    session.flush()
    service = CostLedgerService(session)

    def assert_recovered():
        for filters in ({}, {"chapter_number": 3}, {"candidate_id": "candidate-3"},
                        {"band_id": "band:1:4"},
                        {"chapter_number": 3, "candidate_id": "candidate-3", "band_id": "band:1:4"}):
            report = service.report(project_id=project.id, **filters)
            assert report.totals.attempts == 1
            assert report.totals.total_tokens == 5
            chapter = next(row for row in report.chapters if row.chapter_number == 3)
            assert len(chapter.roles) == 1
            assert chapter.roles[0].role == "planner"
            assert chapter.roles[0].metrics.prompt_tokens == 2
            assert chapter.roles[0].metrics.completion_tokens == 3
            assert [(row.gate_id, row.metrics.attempts) for row in report.gate_costs] == [("band_checkpoint", 1)]
            dimensions = {(row.dimension, row.value) for row in report.dimensions}
            assert {("candidate", "candidate-3"), ("model", "planner-model"),
                    ("provider", "planner-provider")} <= dimensions
        assert service.report(project_id=project.id, candidate_id="absent").totals.attempts == 0

    assert_recovered()
    # Once trace attempts are available, terminal events must no longer add calls.
    trace.attempts_json = json.dumps([attempt])
    session.flush()
    assert_recovered()


@pytest.mark.parametrize("with_trace", [False, True], ids=["event-only", "linked-trace"])
def test_recovered_events_preserve_explicit_event_context(session, with_trace):
    project = Project(title="event context", premise="p", genre="g")
    session.add(project)
    session.flush()
    if with_trace:
        session.add(PromptTrace(
            id="trace", project_id=project.id, stage_key="plan.band", attempts_json="[]",
            input_snapshot_json='{"chapter_number":3,"band_id":"band:1:3","candidate_id":"candidate-3"}',
            model_profile_json='{"model":"planner-model","provider":"planner-provider"}',
        ))
    session.add(DecisionEvent(
        project_id=project.id, chapter_number=4, band_id="band:4:6", event_type=E.LLM_REQUEST_SUCCEEDED,
        payload_json=json.dumps({"prompt_trace_id": "trace", "stage_key": "chapter_review",
                                 "candidate_id": "candidate-4", "gate_id": "review",
                                 "model": "review-model", "provider": "review-provider",
                                 "prompt_tokens": 2, "completion_tokens": 3}),
    ))
    session.flush()
    service = CostLedgerService(session)
    report = service.report(project_id=project.id, chapter_number=4, band_id="band:4:6", candidate_id="candidate-4")
    assert report.totals.attempts == 1 and report.totals.total_tokens == 5
    assert report.chapters[0].roles[0].role == "reviewer"
    assert report.gate_costs[0].gate_id == "review"
    dimensions = {(row.dimension, row.value) for row in report.dimensions}
    assert {("model", "review-model"), ("provider", "review-provider")} <= dimensions
    assert service.report(project_id=project.id, candidate_id="candidate-3").totals.attempts == 0
