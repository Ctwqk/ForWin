"""Protections in force and the protections and milestone refs a chapter delta breaks (C1a)."""

from __future__ import annotations

from typing import Any

import pytest

from forwin.book_state.protections import (
    ActiveProtection,
    DeltaBoundaryViolation,
    active_protections,
    delta_boundary_violations,
)
from forwin.protocol.book_state import GraphDelta, NodePatch
from forwin.world_engine import constraints
from tests.test_world_constraints import _candidate, _change, _node, _protection, _runtime


def _patch(ref: str, path: str, value: Any, *, node_type: str = "character", op: str = "set",
           reason: str = "patch-evidence") -> NodePatch:
    return NodePatch(node_id=ref, node_type=node_type, op=op, field_path=path, old_value=None, new_value=value,
                     reason=reason)


def _delta(*patches: NodePatch, tags: tuple[dict[str, Any], ...] = ()) -> GraphDelta:
    return GraphDelta(id="delta", project_id="p", chapter_number=4, source_type="chapter_extraction",
                      node_patches=list(patches), metadata={"knowledge_tags": list(tags)})


def _tag(observer: str = "b", fact_ref: str = "fact:secret", state: str = "knows", **extra: Any) -> dict[str, Any]:
    observer_type = "reader" if observer == "reader" else "character"
    return {"observer_type": observer_type, "observer_id": observer, "fact_ref": fact_ref, "state": state,
            "channel": "witnessed", "evidence": "tag-evidence", **extra}


def _violations(protection, delta: GraphDelta, *extra_nodes, milestone_refs=frozenset()):
    runtime = _runtime(*extra_nodes, protections=[protection] if protection is not None else [])
    return delta_boundary_violations(runtime, delta, chapter=4, milestone_refs=milestone_refs)


def _broken(protection, impact: str, evidence: str) -> DeltaBoundaryViolation:
    payload = protection.payload
    fact_ref = payload.get("fact_ref", "")
    return DeltaBoundaryViolation(
        protection_node_id=protection.id, milestone_ref="", kind=payload["kind"], ref=payload["ref"],
        observer_ref=payload.get("observer_ref", ""),
        fact_ref=f"fact:{fact_ref.removeprefix('fact:')}" if fact_ref else "",
        field=payload.get("field", ""), value=payload.get("value"), until_chapter=payload["until_chapter"],
        source=payload["source"], impact_level=impact, evidence=evidence,
    )


def _ended(ref: str, field: str, value: str, evidence: str) -> DeltaBoundaryViolation:
    return DeltaBoundaryViolation(
        protection_node_id="", milestone_ref=ref, kind="milestone", ref=ref, observer_ref="", fact_ref="",
        field=field, value=value, until_chapter=0, source="beat_sheet", impact_level="critical", evidence=evidence,
    )


_EXIST_F = _protection("must_exist", "f")
_EXIST_S = _protection("must_exist", "s")
_ALIVE = _protection("must_be_alive")
_HOLDER = _protection("must_hold_state", "item", field="holder_id", value="a")
_FREE = _protection("must_hold_state", field="restraint.state", value="free")
_SECRET = _protection("must_not_know", "b", observer_ref="b", fact_ref="secret")
_PUBLIC = _protection("must_not_know", "b", observer_ref="b", fact_ref="fact:public")
_READER = _protection("must_not_know", "reader", observer_ref="reader", fact_ref="fact:hidden")
_KILL_B = _delta(_patch("b", "state.location_id", "l2"), _patch("b", "state.alive_status", "dead", reason="dies"),
                 _patch("g", "state.status", "besieged", node_type="group"))


@pytest.mark.parametrize(("protection", "delta", "milestone_refs", "expected"), [
    (_EXIST_F, _delta(_patch("f", "state.status", "destroyed", node_type="faction")), frozenset(),
     _broken(_EXIST_F, "major", "patch-evidence")),
    (_EXIST_S, _delta(_patch("s", "state", {"status": "inactive"}, node_type="site_state")), frozenset(),
     _broken(_EXIST_S, "major", "patch-evidence")),
    (_ALIVE, _delta(_patch("a", "state.alive_status", "dead")), frozenset(),
     _broken(_ALIVE, "major", "patch-evidence")),
    (_HOLDER, _delta(_patch("item", "state.holder_id", "b", node_type="item")), frozenset(),
     _broken(_HOLDER, "moderate", "patch-evidence")),
    (_FREE, _delta(_patch("a", "state.restraint", {"state": "imprisoned"})), frozenset(),
     _broken(_FREE, "moderate", "patch-evidence")),
    (_SECRET, _delta(tags=(_tag(),)), frozenset(), _broken(_SECRET, "major", "tag-evidence")),
    (_PUBLIC, _delta(tags=(_tag(fact_ref="fact:public", state="believes"),)), frozenset(),
     _broken(_PUBLIC, "moderate", "tag-evidence")),
    # A flashback still writes cognition.
    (_SECRET, _delta(tags=(_tag(flashback=True),)), frozenset(), _broken(_SECRET, "major", "tag-evidence")),
    (_READER, _delta(tags=(_tag("reader", "fact:hidden"),)), frozenset(), _broken(_READER, "major", "tag-evidence")),
    # A protection on a ref a later milestone needs, or on a fact it needs, is critical.
    (_HOLDER, _delta(_patch("item", "state.holder_id", "b", node_type="item")), frozenset({"item"}),
     _broken(_HOLDER, "critical", "patch-evidence")),
    (_PUBLIC, _delta(tags=(_tag(fact_ref="fact:public"),)), frozenset({"fact:public"}),
     _broken(_PUBLIC, "critical", "tag-evidence")),
    # A milestone ref is broken only by ending it: death or an ended status.
    (None, _KILL_B, frozenset({"b", "g", "fact:secret", "missing"}),
     _ended("b", "state.alive_status", "dead", "dies")),
    (None, _delta(_patch("g", "state.status", "disbanded", node_type="group", reason="disbands")), frozenset({"g"}),
     _ended("g", "state.status", "disbanded", "disbands")),
])
def test_delta_violations_cover_four_kinds_and_milestone(protection, delta, milestone_refs, expected):
    assert _violations(protection, delta, milestone_refs=milestone_refs) == [expected]


@pytest.mark.parametrize(("protection", "delta", "extra"), [
    # Dying and coming back in one chapter leaves the protected state as it was.
    (_protection("must_be_alive"), _delta(_patch("a", "state.alive_status", "dead"),
                                          _patch("a", "state.alive_status", "alive")), ()),
    (_protection("must_be_alive"), _delta(_patch("a", "state.alive_status", "dead", op="create")), ()),
    (_protection("must_be_alive"), _delta(_patch("b", "state.alive_status", "dead")), ()),
    (_protection("must_exist", "s"), _delta(_patch("s", "state.status", "inactive", node_type="site_state")),
     (_node("s", "site_state", status="destroyed"),)),
    (_protection("must_exist", "f"), _delta(_patch("f", "state.status", "besieged", node_type="faction")), ()),
    (_protection("must_hold_state", field="restraint.state", value="free"),
     _delta(_patch("a", "state.restraint", {"state": "free", "by_ref": "b"})), ()),
    (_protection("must_not_know", "b", observer_ref="b", fact_ref="fact:secret"),
     _delta(tags=(_tag(state="suspects"), _tag("c"), _tag(fact_ref="fact:public"))), ()),
    (_protection("must_not_know", "reader", observer_ref="reader", fact_ref="fact:secret"),
     _delta(tags=(_tag("b"),)), ()),
    (_protection("must_be_alive", until_chapter=3), _delta(_patch("a", "state.alive_status", "dead")), ()),
    (_protection("must_be_alive", status="released"), _delta(_patch("a", "state.alive_status", "dead")), ()),
])
def test_delta_respects_kept_states_weak_knowledge_and_expired_protections(protection, delta, extra):
    assert _violations(protection, delta, *extra) == []


def test_only_the_reader_learns_a_secret_by_touching_it():
    touching = _delta(tags=(
        _tag(fact_ref="fact:public", touches_secret_ref="fact:hidden"),
        _tag("reader", "fact:public", touches_secret_ref="fact:hidden"),
    ))
    actor = _protection("must_not_know", "b", observer_ref="b", fact_ref="fact:hidden")

    # Touching a secret is not knowing it; the reader, though, is shown what the prose touches.
    assert _violations(actor, touching) == []
    assert _violations(_READER, touching) == [_broken(_READER, "major", "tag-evidence")]


def test_planned_ending_is_not_a_milestone_violation():
    kill = _delta(_patch("b", "state.alive_status", "dead"),
                  _patch("g", "state.status", "disbanded", node_type="group"))
    runtime = _runtime()

    def ended(planned_endings):
        return [(v.ref, v.value) for v in delta_boundary_violations(
            runtime, kill, chapter=4, milestone_refs=frozenset({"b", "g"}), planned_endings=planned_endings,
        )]

    assert ended(frozenset()) == [("b", "dead"), ("g", "disbanded")]
    # The exit state's own ending is the arc's goal; another ending of an exit-state ref still counts.
    assert ended(frozenset({("b", "state.alive_status", "dead"), ("g", "state.status", "destroyed")})) == [
        ("g", "disbanded"),
    ]
    assert ended(frozenset({("b", "state.alive_status", "dead"), ("g", "state.status", "disbanded")})) == []


def test_protected_refs_read_shared_protections():
    from forwin.world_engine.tick_nodes import protected_refs

    runtime = _runtime()
    runtime.narrative.nodes_by_id = {node.id: node for node in (
        _protection("must_be_alive", until_chapter=0).model_copy(update={"id": "open"}),
        _protection("must_hold_state", "item", field="holder_id", value="a").model_copy(update={"id": "held"}),
        _protection("must_exist", "f", status="released").model_copy(update={"id": "released"}),
        _protection("must_exist", "g", until_chapter=3).model_copy(update={"id": "expired"}),
        _protection("must_not_know", "b", observer_ref="b", fact_ref="secret").model_copy(update={"id": "knowledge"}),
    )}

    assert protected_refs(runtime, next_chapter=4) == ["a", "item"]
    assert protected_refs(runtime, next_chapter=5) == ["a"]


def test_active_protections_read_each_payload_once():
    nodes = [
        _protection("must_be_alive", until_chapter=0).model_copy(update={"id": "open"}),
        _protection("must_be_alive", until_chapter="5").model_copy(update={"id": "non-integer"}),
        _protection("must_be_alive", until_chapter=3).model_copy(update={"id": "expired"}),
        _protection("must_be_alive").model_copy(update={"id": "inactive", "status": "inactive"}),
        _protection("must_be_alive", status="released").model_copy(update={"id": "released"}),
        _protection("must_hold_state").model_copy(update={"id": "no-field"}),
        _protection("unknown").model_copy(update={"id": "unknown-kind"}),
        _protection("must_be_alive", field=["unused"]).model_copy(update={"id": "unused-field"}),
        _protection("must_not_know", "", observer_ref="b", fact_ref="secret").model_copy(update={"id": "knowledge"}),
        _protection("must_not_know", "b", observer_ref=[], fact_ref="secret").model_copy(update={"id": "no-observer"}),
    ]
    runtime = _runtime()
    runtime.narrative.nodes_by_id = {node.id: node for node in nodes}

    assert active_protections(runtime, chapter=4) == [
        ActiveProtection(node_id="knowledge", kind="must_not_know", ref="b", until_chapter=4, source="test",
                         observer_ref="b", fact_ref="fact:secret"),
        ActiveProtection(node_id="non-integer", kind="must_be_alive", ref="a", until_chapter=0, source="test"),
        ActiveProtection(node_id="open", kind="must_be_alive", ref="a", until_chapter=0, source="test"),
        ActiveProtection(node_id="unused-field", kind="must_be_alive", ref="a", until_chapter=4, source="test"),
    ]


def test_tick_boundary_check_reads_shared_protections(monkeypatch):
    protection = _protection("must_hold_state", "item", field="holder_id", value="a")
    runtime = _runtime(protections=[protection])
    tick = constraints.check_narrative_boundaries(
        runtime, _candidate({"item_transfers": [{"item_ref": "item", "from_ref": "a", "to_ref": "b"}]}),
        next_chapter=4,
    )
    chapter = delta_boundary_violations(runtime, _delta(_patch("item", "state.holder_id", "b", node_type="item")),
                                        chapter=4)
    assert [(v.constraint_node_id, v.kind, v.ref) for v in tick] == [
        (v.protection_node_id, v.kind, v.ref) for v in chapter
    ] == [("protection", "must_hold_state", "item")]

    # The tick takes its protections from the shared reader, not from the payloads.
    shared = [ActiveProtection(node_id="shared", kind="must_be_alive", ref="b", until_chapter=0, source="human")]
    seen = []

    def read(runtime_arg, *, chapter):
        seen.append(chapter)
        return shared

    monkeypatch.setattr(constraints, "active_protections", read)
    violations = constraints.check_narrative_boundaries(_runtime(), _candidate(_change("b", "alive_status", "dead")),
                                                        next_chapter=7)
    assert [(v.constraint_node_id, v.kind, v.ref) for v in violations] == [("shared", "must_be_alive", "b")]
    assert seen == [7]
