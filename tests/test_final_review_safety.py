"""FU-R49/FU-R50 regressions that need no PostgreSQL canon writes."""

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.extraction.translator import _DeltaBuilder
from forwin.book_state.reviewer import _immutable_rule_definition_issues
from forwin.generation.pipeline_core.repair_patches import _current_chapter_repair_experience_plan
from forwin.protocol.book_state import FactNode, GraphDelta, NodePatch, WorldNode
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.review.draft_service import DraftReviewService
from forwin.review.world_consistency import WorldConsistencyReviewer, _Finding
from forwin.world_engine.conflict_resolution import ConflictOptionUnavailable, record_option_side_effects
from forwin.world_engine.conflict_routing import available_options
from forwin.world_engine.tick import _Tick
from forwin.writer.prompt_core.world_repair import WORLD_REPAIR_FIXES
from forwin.writer.prompt_core.world_view import UNNAMED_ACTOR_LABEL, render_world_view
from tests.test_chapter_world_view import source, _build, _plan
from tests.test_world_conflict_resolution import pending_side_effect_session, _decision
from tests.test_world_conflict_routing import _draft
from tests.test_writer_world_view_prompt import BUILDERS, _context, _prompt


@pytest.mark.parametrize("message,code", [
    ("ghost tells fact:forbidden without knowing it (state: unknown)", "told_by_unaware_source"),
    ("blade is handed over by hero but is held by ghost", "item_holder_mismatch"),
])
def test_concealed_review_names_stay_in_audit(source, message, code):
    world = source.runtime.world
    world.add_node(WorldNode(id="ghost", project_id="p", node_type="character", name="CONCEALED_REAL_NAME",
                             state={"visibility_level": "hidden"}))
    secret_name = world.nodes_by_id["ghost"].name
    translator = object.__new__(_DeltaBuilder)
    translator.world, translator.map = world, source.runtime.map
    translator.new_sites, translator.created_nodes, translator.fact_patches = {}, {}, []
    names = translator._display_names()
    assert names["ghost"] == UNNAMED_ACTOR_LABEL
    issue = WorldConsistencyReviewer()._issue(
        _Finding("error", code, message, "ghost", "BookStateReviewGate"),
        names, translator._fact_topics(), translator._concealed_names(),
    )
    assert issue.original_result["concealed_entity_names"] == {"ghost": secret_name}
    assert secret_name not in issue.entity_names
    assert UNNAMED_ACTOR_LABEL not in issue.entity_names
    context = _context(_build(source), chapter_experience_plan=ChapterExperiencePlan())
    instruction = DraftReviewService._continuity_repair_instruction(continuity_issues=[issue], context=context)
    overlay = _current_chapter_repair_experience_plan(context.chapter_experience_plan, instruction)
    assert instruction.must_fix and overlay.rule_anchors
    assert UNNAMED_ACTOR_LABEL in issue.description
    for text in (issue.description, issue.suggested_fix, *instruction.must_fix, *overlay.rule_anchors):
        assert secret_name not in text
    for builder in BUILDERS:
        assert secret_name not in _prompt(builder, context.model_copy(update={"chapter_experience_plan": overlay}))


@pytest.mark.parametrize("visibility,name", [
    ("hidden", "CONCEALED_RULE_NAME_FU_R50"),
    ("open", "OPEN_RULE_NAME_FU_R50"),
])
def test_rule_definition_gate_respects_name_visibility_through_writer_repair(source, visibility, name):
    rule = WorldNode(
        id="rule-definition", project_id="p", node_type="rule", name=name,
        profile={"statement": "old rule"}, state={"visibility_level": visibility},
    )
    source.runtime.world.add_node(rule)
    delta = GraphDelta(
        id="rule-change", project_id="p", chapter_number=6, source_type="chapter_extraction",
        node_patches=[NodePatch(
            node_id=rule.id, node_type="rule", op="set", field_path="profile.statement",
            old_value="old rule", new_value="changed rule",
        )],
    )
    gate_issue, = _immutable_rule_definition_issues(source.runtime, delta)
    assert gate_issue.code == "immutable_rule_definition_conflict"
    translator = object.__new__(_DeltaBuilder)
    translator.world, translator.map = source.runtime.world, source.runtime.map
    translator.new_sites, translator.created_nodes, translator.fact_patches = {}, {}, []
    issue = WorldConsistencyReviewer()._issue(
        _Finding(gate_issue.severity, gate_issue.code, gate_issue.message, gate_issue.target_ref, "BookStateReviewGate"),
        translator._display_names(), translator._fact_topics(), translator._concealed_names(),
    )
    plan = _plan(onstage_refs_json='["hero", "ally"]', reveal_directives_json="[]")
    context = _context(_build(source, plan=plan), chapter_experience_plan=ChapterExperiencePlan())
    instruction = DraftReviewService._continuity_repair_instruction(continuity_issues=[issue], context=context)
    overlay = _current_chapter_repair_experience_plan(context.chapter_experience_plan, instruction)
    assert issue.suggested_fix and instruction.must_fix and overlay.rule_anchors
    prompts = [
        _prompt(builder, context.model_copy(update={"chapter_experience_plan": overlay}))
        for builder in BUILDERS
    ]
    if visibility == "hidden":
        for text in (issue.description, issue.suggested_fix, *issue.entity_names,
                     *instruction.must_fix, *overlay.rule_anchors, *prompts):
            assert name not in text
        assert UNNAMED_ACTOR_LABEL in issue.description
        assert issue.entity_names == []
        assert issue.original_result["concealed_entity_names"] == {rule.id: name}
        assert name in json.dumps(issue.original_result)
    else:
        assert name in issue.description
        assert issue.entity_names == [name]
        assert all(name in text for text in (*instruction.must_fix, *overlay.rule_anchors, *prompts))
        assert issue.original_result["concealed_entity_names"] == {}
    assert rule.id in gate_issue.message
    assert name not in gate_issue.message


def test_raw_concealed_names_are_redacted_from_review_description_and_fix(monkeypatch):
    name = "CONCEALED_RULE_NAME_FU_R50"
    longer_name = f"{name}_EXTENDED"
    code = "immutable_rule_definition_conflict"
    message = f"prefix{longer_name}suffix; {name}; {longer_name}"
    monkeypatch.setitem(WORLD_REPAIR_FIXES, code, f"Keep {longer_name}; preserve {name}.")
    issue = WorldConsistencyReviewer()._issue(
        _Finding("error", code, message, "rule-hidden", "BookStateReviewGate"),
        {"rule-hidden": UNNAMED_ACTOR_LABEL}, {},
        {"rule-hidden": name, "rule-extended": longer_name, "rule-blank": ""},
    )
    assert issue.description == f"prefix{UNNAMED_ACTOR_LABEL}suffix; {UNNAMED_ACTOR_LABEL}; {UNNAMED_ACTOR_LABEL}"
    assert issue.suggested_fix == f"Keep {UNNAMED_ACTOR_LABEL}; preserve {UNNAMED_ACTOR_LABEL}."
    assert issue.entity_names == []
    assert issue.original_result["message"] == message
    assert issue.original_result["concealed_entity_names"] == {"rule-hidden": name}


def test_forbidden_topic_lines_deduplicate_without_losing_fact_refs(source):
    for suffix in ("a", "b"):
        source.runtime.world.add_fact(FactNode(
            id=f"duplicate-{suffix}", project_id="p", proposition=f"secret-{suffix}",
            sensitivity_level="hidden", related_node_refs=["ally"],
        ))
    view = _build(source)
    topics = {item.fact_ref: item.topic for item in view.forbidden_topics}
    topic = topics["fact:duplicate-a"]
    assert topics["fact:duplicate-b"] == topic
    assert render_world_view(view).splitlines().count(f"    · {topic}") == 1


def test_plot_response_requires_a_reachable_chapter():
    assert available_options(_draft(impact_level="moderate"), live_intent=True, reachable_chapter=False) == (
        "veto_world", "amend_plan", "accept_with_obligation",
    )


def test_tick_conflict_omits_plot_response_at_book_target(pending_side_effect_session, source):
    from forwin.runtime.policy import RuntimePolicy
    from forwin.world_engine.tick_conflicts import TickConflicts
    from forwin.world_engine.types import TickContext

    session, row = pending_side_effect_session
    source.runtime.world.add_node(WorldNode(id="intent-1", project_id=row.project_id, node_type="intent",
                                           name="intent", state={"status": "planned"}))
    policy = RuntimePolicy.for_profile("standard")
    policy = policy.model_copy(update={"world": policy.world.model_copy(update={"conflict_delegate": "human"})})
    conflicts = TickConflicts(
        session, context=TickContext(project_id=row.project_id, chapter_number=20, now=100, window_start=0, seed=1),
        runtime=source.runtime, policy=policy, tick_id="final", delegate=None, audit_writer_factory=lambda: None,
    )
    conflicts.hold(_draft().model_copy(update={"project_id": row.project_id, "detected_after_chapter": 20,
                                              "blocks_from_chapter": 21}), extra={})
    offered = conflicts.repo.get(row.project_id, conflicts.conflict_ids[0])
    assert json.loads(offered.options_json) == ["veto_world", "amend_plan"]


def test_exhausted_plot_response_names_remaining_options(pending_side_effect_session):
    session, row = pending_side_effect_session
    with pytest.raises(ConflictOptionUnavailable) as error:
        record_option_side_effects(session, row, _decision("plot_responds"), latest_chapter=20)
    assert "Remaining options: veto_world, amend_plan" in str(error.value)


def test_cancelled_onstage_resolution_does_not_stage_its_intent():
    tick = object.__new__(_Tick)
    candidate = SimpleNamespace(candidate_id="candidate", origin="conflict_resolution", actor_id="actor", intent_id="intent")
    issue = SimpleNamespace(code="touches_onstage_entity", message="onstage change")
    check = SimpleNamespace(admit=lambda _: [issue], mark=lambda: "mark", reset=lambda _: None)
    tick._running_check = lambda: check
    cancelled, dropped = [], []
    tick.conflicts = SimpleNamespace(resolutions=[SimpleNamespace(conflict_id="conflict", candidates=[candidate])],
                                     cancel=lambda *a, **kw: cancelled.append((a, kw)))
    tick._drop = lambda *a, **kw: dropped.append((a, kw))
    tick._drop_candidate = lambda *a, **kw: SimpleNamespace(staged=True)
    # A staged candidate would be admitted; the original rejected resolution is not.
    check.admit = lambda item: [] if getattr(item, "staged", False) else [issue]
    assert tick._admitted([]) == []
    assert cancelled[0][0] == ("conflict",)
    assert dropped[0][1]["conflict_id"] == "conflict"


def test_http_resolution_seeds_latest_chapter_and_onstage_refs(monkeypatch, source):
    from forwin.world_engine import conflict_resolution as module
    from forwin.world_engine.running_state import RunningStateCheck
    from forwin.world_engine.types import TickContext
    from forwin.runtime.policy import RuntimePolicy

    chapter = SimpleNamespace(source_type="chapter_extraction", metadata={"movements": [
        {"entity_id": "mover", "arrive_minute": 90},
    ]}, node_patches=[])
    older = SimpleNamespace(source_type="chapter_extraction", metadata={}, node_patches=[])
    tick_delta = SimpleNamespace(source_type="world_engine", metadata={}, node_patches=[])
    monkeypatch.setattr(module.BookStateRepository, "list_graph_deltas", lambda *a, **k: [older, chapter, tick_delta])
    monkeypatch.setattr(module.BookLexiconRepository, "get", lambda *a: source.lexicon)
    monkeypatch.setattr(module.StoryCalendarRepository, "get", lambda *a: None)
    checked = []

    def admit(check, candidate):
        assert check.onstage_refs == frozenset({"cast", "pov", "mover"})
        assert check.last_arrival_by_mover == {"mover": 90}
        checked.append(candidate)
        return []

    monkeypatch.setattr(RunningStateCheck, "admit", admit)
    session = SimpleNamespace(scalar=lambda *a: SimpleNamespace(onstage_refs_json='["cast"]', pov_ref="pov"))
    service = module.ConflictResolutionService(session, admission=None, policy=RuntimePolicy.for_profile("standard"))
    # Production pairs the latest-canon runtime with a context for the same chapter.
    latest = source.runtime.as_of_chapter
    service._check_against_canon(SimpleNamespace(project_id="p"), ["candidate"], runtime=source.runtime,
                                 context=TickContext(project_id="p", chapter_number=latest, now=100, window_start=0,
                                                     seed=1),
                                 tick_id="resolution")
    assert checked == ["candidate"]
