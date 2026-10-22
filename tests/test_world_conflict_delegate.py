from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.generation.gate_delegation import GateAuditWriter
from forwin.models import Base, Project
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.protocol.book_state import FactNode, MapNode, WorldNode
from forwin.runtime.policy import ModelsPolicy, RuntimePolicy
from forwin.world_engine import conflict_delegate, conflict_routing
from forwin.world_engine.conflict_delegate import WorldConflictDelegate, build_conflict_context_snapshot
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_routing import available_options, route_conflict
from forwin.world_engine.conflict_types import ConflictDraft
from forwin.writer.prompt_core import world_engine as world_engine_prompts
from forwin.writer.prompt_core.world_engine import build_conflict_review_messages


PROJECT_ID = "project-1"
ASTRA = "gpt-6-astra"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id=PROJECT_ID, title="Test", premise="Test", genre="test"))
        session.commit()
        yield session
    engine.dispose()


def _draft(**overrides) -> ConflictDraft:
    return ConflictDraft(**{
        "project_id": PROJECT_ID,
        "source": "world_tick",
        "detected_after_chapter": 3,
        "story_time": 3_000,
        "involved_refs": ["actor-1", "fact:fact-1"],
        "violated": "impact_review",
        "impact_level": "major",
        "summary": "The actor would leave the protected place.",
        "proposed_change": {"candidate_id": "actor_turn:actor-1"},
        "intent_ref": "intent-1",
        "actor_ref": "actor-1",
        "blocks_from_chapter": 4,
        **overrides,
    })


def _policy(**role_preferences) -> RuntimePolicy:
    policy = RuntimePolicy.for_profile("standard")
    if not role_preferences:
        return policy
    return policy.model_copy(update={"models": ModelsPolicy(role_preferences=role_preferences)})


def _conflict(session: Session, **overrides):
    draft = _draft(**overrides)
    policy = _policy()
    row = WorldConflictRepository(session).create(
        draft, required_decider=route_conflict(draft, policy=policy), options=available_options(draft),
    )
    session.commit()
    return row


class RecordingUpdater:
    def __init__(self) -> None:
        self.events = []
        self.traces: list[dict[str, object]] = []

    def save_decision_event(self, event):
        self.events.append(event)
        return SimpleNamespace(
            id=f"event-{len(self.events)}",
            causal_root_id=event.causal_root_id or f"event-{len(self.events)}",
        )

    def save_prompt_trace(self, **payload):
        self.traces.append(payload)
        return SimpleNamespace(id=f"trace-{len(self.traces)}")

    def writer(self) -> GateAuditWriter:
        return GateAuditWriter(
            save_decision_event=self.save_decision_event,
            save_prompt_trace=self.save_prompt_trace,
        )

    def event_types(self) -> list[str]:
        return [event.event_type for event in self.events]


class FakeCodexLLM:
    def __init__(
        self,
        content: str,
        *,
        actual_model: str = ASTRA,
        backend: str = "codex_bridge",
        error: Exception | None = None,
    ) -> None:
        self.content = content
        self.actual_model = actual_model
        self.backend = backend
        self.error = error
        self.last_call_result = None
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        self.last_call_result = SimpleNamespace(
            backend=self.backend,
            fallback_used=False,
            trace={"backend": self.backend, "actual_model": self.actual_model},
        )
        if self.error is not None:
            raise self.error
        return self.content

    def drain_llm_attempt_events(self):
        return []


def _answer(option: str, **overrides) -> str:
    return json.dumps({
        "option": option,
        "rationale": "The volume goal still holds if the next chapter answers the move.",
        "risk_level": "low",
        "evidence": ["actor-1"],
        **overrides,
    })


def _snapshot(conflict) -> dict:
    return {
        "conflict": {"id": conflict.id, "summary": conflict.summary, "options": json.loads(conflict.options_json)},
        "book_state": [{
            "ref": "actor-1",
            "kind": "world_node",
            "value": WorldNode(
                id="actor-1", project_id=PROJECT_ID, node_type="character", name="actor-one",
                state={"location_id": "place-1"},
            ).model_dump(mode="json"),
        }],
        "arc_plan": {"goal": "arc-goal", "stakes": "arc-stakes"},
        "upcoming_chapters": [{"chapter_number": 4, "title": "chapter-four", "goals": ["goal-a"]}],
    }


def _decide(session: Session, llm: FakeCodexLLM, updater: RecordingUpdater, *, policy=None, **overrides):
    conflict = _conflict(session, **overrides)
    delegation = WorldConflictDelegate(llm).decide(
        conflict=conflict,
        context_snapshot=_snapshot(conflict),
        policy=policy or _policy(),
        audit_writer=updater.writer(),
    )
    return conflict, delegation


def test_delegate_returns_option_from_codex(session: Session) -> None:
    updater = RecordingUpdater()
    llm = FakeCodexLLM(_answer("plot_responds"))

    conflict, delegation = _decide(session, llm, updater, impact_level="moderate")

    assert delegation.option == "plot_responds"
    assert delegation.escalate is False
    assert delegation.failure_reason == ""
    assert delegation.rationale.startswith("The volume goal")
    assert delegation.trace_id == "trace-1"
    call = llm.calls[0]
    assert call["preferred_provider_kind"] == "codex"
    assert call["preferred_model"] == ASTRA
    assert call["stage_key"] == "review.world_conflict"
    assert call["task_family"] == "review"
    assert call["permission_profile"] == "prompt_only_readonly"
    assert call["output_schema"]["properties"]["option"]["enum"] == json.loads(conflict.options_json)
    trace = updater.traces[0]
    assert trace["stage_key"] == "review.world_conflict"
    assert trace["trace_scope"] == "world_engine"
    assert conflict.id in str(trace["input_snapshot_json"])
    assert updater.event_types() == ["prompt_trace_recorded"]
    assert updater.events[0].related_object_id == "trace-1"
    session.refresh(conflict)
    assert conflict.status == "open"
    assert conflict.required_decider == "codex"


@pytest.mark.parametrize(
    ("role_preferences", "expected_model"),
    [({"review.world_conflict": ("gpt-5.6-sol",)}, "gpt-5.6-sol"), ({"review.world_conflict": ()}, ASTRA)],
)
def test_delegate_model_comes_from_role_preferences(
    session: Session, role_preferences: dict, expected_model: str
) -> None:
    updater = RecordingUpdater()
    llm = FakeCodexLLM(_answer("veto_world"), actual_model=expected_model)

    _conflict_row, delegation = _decide(session, llm, updater, policy=_policy(**role_preferences))

    assert llm.calls[0]["preferred_model"] == expected_model
    assert delegation.option == "veto_world"
    assert delegation.escalate is False


@pytest.mark.parametrize(
    ("llm_kwargs", "expected_failure"),
    [
        ({"content": "partial", "error": RuntimeError("provider timeout")}, "llm_call_failed"),
        ({"content": _answer("veto_world"), "actual_model": "gpt-5.6-sol"}, "model_mismatch"),
        ({"content": _answer("veto_world"), "backend": "openai_compatible"}, "model_mismatch"),
        # A major conflict never offers accept_with_obligation, and P3 never offers rewrite_chapter.
        ({"content": _answer("accept_with_obligation")}, "parse_or_schema"),
        ({"content": _answer("rewrite_chapter")}, "parse_or_schema"),
        ({"content": json.dumps({"option": "veto_world"})}, "parse_or_schema"),
        ({"content": "not-json"}, "parse_or_schema"),
    ],
)
def test_delegate_failures_escalate_without_applying(
    session: Session, llm_kwargs: dict, expected_failure: str
) -> None:
    updater = RecordingUpdater()
    llm = FakeCodexLLM(**llm_kwargs)

    conflict, delegation = _decide(session, llm, updater)

    assert delegation.option is None
    assert delegation.escalate is True
    assert delegation.failure_reason == expected_failure
    assert delegation.trace_id == "trace-1"
    assert updater.event_types() == ["prompt_trace_recorded", "world_conflict_escalated"]
    escalated = updater.events[-1]
    assert escalated.payload["conflict_id"] == conflict.id
    assert escalated.payload["failure_reason"] == expected_failure
    assert escalated.payload["trace_id"] == "trace-1"
    assert escalated.parent_event_id == "event-1"
    assert escalated.chapter_number == conflict.detected_after_chapter
    assert len(updater.traces) == 1
    assert expected_failure in str(updater.traces[0]["output_summary_json"])
    session.expire_all()
    stored = WorldConflictRepository(session).get(PROJECT_ID, conflict.id)
    assert stored.status == "open"
    assert stored.required_decider == "codex"
    assert stored.decision_json == "{}"


def test_amend_plan_choice_escalates(session: Session) -> None:
    updater = RecordingUpdater()
    llm = FakeCodexLLM(_answer("amend_plan", risk_level="high"))

    conflict, delegation = _decide(session, llm, updater)

    assert delegation.option == "amend_plan"
    assert delegation.escalate is True
    assert delegation.failure_reason == ""
    assert delegation.rationale.startswith("The volume goal")
    assert updater.event_types() == ["prompt_trace_recorded", "world_conflict_escalated"]
    escalated = updater.events[-1]
    assert escalated.payload["conflict_id"] == conflict.id
    assert escalated.payload["option"] == "amend_plan"
    assert escalated.payload["risk_level"] == "high"
    assert escalated.parent_event_id == "event-1"
    session.refresh(conflict)
    assert conflict.status == "open"


def test_prompt_too_large_escalates_without_calling_codex(session: Session) -> None:
    updater = RecordingUpdater()
    llm = FakeCodexLLM(_answer("veto_world"))
    conflict = _conflict(session)
    snapshot = _snapshot(conflict)
    # Neither a default-valued field nor a goal: no reduction step can drop it.
    snapshot["book_state"][0]["value"]["description"] = "d" * (world_engine_prompts.MAX_CONFLICT_REVIEW_PROMPT_CHARS + 1)

    delegation = WorldConflictDelegate(llm).decide(
        conflict=conflict, context_snapshot=snapshot, policy=_policy(), audit_writer=updater.writer(),
    )

    assert llm.calls == []
    assert delegation.option is None
    assert delegation.escalate is True
    assert delegation.failure_reason == "prompt_too_large"
    assert delegation.trace_id == ""
    assert updater.traces == []
    assert updater.event_types() == ["world_conflict_escalated"]
    assert updater.events[0].payload["failure_reason"] == "prompt_too_large"
    assert updater.events[0].related_object_id == conflict.id


@pytest.mark.parametrize("options_json", ["[]", ""], ids=["empty_list", "empty_string"])
def test_empty_options_escalate_without_calling_codex(session: Session, options_json: str) -> None:
    updater = RecordingUpdater()
    llm = FakeCodexLLM(_answer("veto_world"))
    conflict = _conflict(session)
    conflict.options_json = options_json

    delegation = WorldConflictDelegate(llm).decide(
        conflict=conflict, context_snapshot={}, policy=_policy(), audit_writer=updater.writer(),
    )

    assert delegation.option is None
    assert delegation.escalate is True
    assert delegation.failure_reason == "no_options"
    assert llm.calls == []
    assert updater.traces == []
    assert updater.event_types() == ["world_conflict_escalated"]
    assert updater.events[0].payload["failure_reason"] == "no_options"
    assert updater.events[0].related_object_id == conflict.id
    assert conflict.status == "open"
    assert conflict.required_decider == "codex"


def test_review_prompt_drops_default_fields_then_truncates_goals(monkeypatch) -> None:
    node = WorldNode(id="actor-1", project_id=PROJECT_ID, node_type="character", name="actor-one")
    long_goal = "g" * 5000
    snapshot = {
        "conflict": {"id": "conflict-1", "summary": "summary"},
        "book_state": [{"ref": "actor-1", "kind": "world_node", "value": node.model_dump(mode="json")}],
        "upcoming_chapters": [{"chapter_number": 4, "title": "chapter-four", "goals": [long_goal]}],
    }
    schema = {"type": "object"}

    def user_payload() -> dict:
        messages = build_conflict_review_messages(
            context_snapshot=snapshot, options=["veto_world"], output_schema=schema,
        )
        return json.loads(messages[-1]["content"])

    full = user_payload()
    assert full["book_state"][0]["value"]["importance"] == 5
    assert full["upcoming_chapters"][0]["goals"] == [long_goal]
    assert full["options"] == ["veto_world"]

    full_chars = len(json.dumps(full, ensure_ascii=False))
    monkeypatch.setattr(world_engine_prompts, "MAX_CONFLICT_REVIEW_PROMPT_CHARS", full_chars - 1)
    compact = user_payload()
    assert "importance" not in compact["book_state"][0]["value"]
    assert compact["book_state"][0]["value"]["name"] == "actor-one"
    assert compact["upcoming_chapters"][0]["goals"] == [long_goal]

    monkeypatch.setattr(world_engine_prompts, "MAX_CONFLICT_REVIEW_PROMPT_CHARS", 2000)
    truncated = user_payload()
    goal = truncated["upcoming_chapters"][0]["goals"][0]
    assert long_goal.startswith(goal)
    assert len(goal) == world_engine_prompts.MAX_CONFLICT_REVIEW_GOAL_CHARS
    assert snapshot["upcoming_chapters"][0]["goals"] == [long_goal]


def test_build_conflict_context_snapshot_reads_involved_refs_as_of_detection(session: Session) -> None:
    repo = BookStateRepository(session)
    actor = WorldNode(
        id="actor-1", project_id=PROJECT_ID, node_type="character", name="actor-one",
        state={"location_id": "place-1"},
    )
    repo.create_world_node(actor, version_chapter=0, version_state={"location_id": "place-1"})
    repo.create_world_node(actor, version_chapter=5, version_state={"location_id": "place-2"})
    bystander = WorldNode(id="bystander-1", project_id=PROJECT_ID, node_type="character", name="bystander")
    repo.create_world_node(bystander, version_chapter=0, version_state={})
    repo.create_fact_node(FactNode(id="fact-1", project_id=PROJECT_ID, proposition="involved-fact"))
    repo.create_fact_node(FactNode(id="fact-2", project_id=PROJECT_ID, proposition="hidden-fact"))
    repo.create_map_node(MapNode(id="place-1", project_id=PROJECT_ID, node_type="site", name="place-one"))
    arc = ArcPlanVersion(
        project_id=PROJECT_ID, arc_synopsis="arc-synopsis", goal="arc-goal", stakes="arc-stakes",
        payoff_direction="", status="active", chapter_start=1, chapter_end=10,
    )
    session.add(arc)
    session.flush()
    statuses = {1: "accepted", 2: "accepted", 3: "accepted", 4: "accepted", 5: "planned", 6: "failed", 7: "planned", 8: "planned"}
    for number, status in statuses.items():
        session.add(ChapterPlan(
            project_id=PROJECT_ID, arc_plan_id=arc.id, chapter_number=number, title=f"chapter-{number}",
            goals_json=json.dumps([f"goal-{number}"]), status=status,
        ))
    session.flush()
    conflict = _conflict(session, involved_refs=["actor-1", "fact:fact-1", "place-1", "missing-ref"])

    snapshot = build_conflict_context_snapshot(session, conflict=conflict)

    record = snapshot["conflict"]
    assert record["id"] == conflict.id
    assert record["options"] == json.loads(conflict.options_json)
    assert record["involved_refs"] == ["actor-1", "fact:fact-1", "place-1", "missing-ref"]
    assert record["impact_level"] == "major"
    slices = {item["ref"]: item for item in snapshot["book_state"]}
    assert set(slices) == {"actor-1", "fact:fact-1", "place-1"}
    assert slices["actor-1"]["kind"] == "world_node"
    assert slices["actor-1"]["value"]["state"] == {"location_id": "place-1"}
    assert slices["fact:fact-1"]["kind"] == "fact"
    assert slices["fact:fact-1"]["value"]["proposition"] == "involved-fact"
    assert slices["place-1"]["kind"] == "map_node"
    text = json.dumps(snapshot, ensure_ascii=False)
    assert "hidden-fact" not in text
    assert "bystander" not in text
    assert snapshot["arc_plan"] == {"goal": "arc-goal", "stakes": "arc-stakes", "synopsis": "arc-synopsis"}
    assert snapshot["upcoming_chapters"] == [
        {"chapter_number": 5, "title": "chapter-5", "goals": ["goal-5"]},
        {"chapter_number": 6, "title": "chapter-6", "goals": ["goal-6"]},
        {"chapter_number": 7, "title": "chapter-7", "goals": ["goal-7"]},
    ]


def test_build_conflict_context_snapshot_omits_missing_plans(session: Session) -> None:
    conflict = _conflict(session, involved_refs=["missing-ref"])

    snapshot = build_conflict_context_snapshot(session, conflict=conflict)

    assert set(snapshot) == {"conflict"}


@pytest.mark.parametrize("module", [conflict_delegate, conflict_routing], ids=["delegate", "routing"])
def test_world_conflict_delegate_module_has_no_canon_write_surface(module) -> None:
    source = Path(module.__file__).read_text()

    for forbidden in (
        "forwin.canon",
        "CandidateDraftRepository",
        "CanonCommitRecord",
        "GraphDeltaRow",
        "EntityAdmissionCommitter",
        ".commit_plan(",
        ".mark_chapter_status(",
        "StateUpdater",
        ".session",
        # The caller escalates or resolves the conflict.
        "WorldConflictRepository",
        ".escalate(",
        ".mark_resolved(",
    ):
        assert forbidden not in source
