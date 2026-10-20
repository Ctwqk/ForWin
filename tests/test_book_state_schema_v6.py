from __future__ import annotations

import pytest

from forwin.book_state.schema import validate_graph_delta, validate_world_edge, validate_world_node
from forwin.protocol.book_state import GraphDelta, NodePatch, WorldEdge, WorldNode


def _node(node_type: str) -> WorldNode:
    payloads = {
        "agenda": ({"owner_ref": "node:actor", "goal": "A goal"}, {"status": "active", "priority": 1}),
        "intent": (
            {"owner_ref": "node:actor", "action_type": "move", "impact_level": "minor", "requires_onstage": False},
            {"status": "planned", "scheduled_at": 0, "slip_count": 0},
        ),
        "clock": ({"label": "A deadline", "scope": "public"}, {"due_at": 0, "status": "running"}),
    }
    profile, state = payloads.get(node_type, ({}, {}))
    return WorldNode(id="node-1", project_id="project-1", node_type=node_type, profile=profile, state=state)


def _delta(node_type: str, op: str, value: object, field_path: str = "") -> GraphDelta:
    return GraphDelta(
        id="delta-1",
        project_id="project-1",
        node_patches=[
            NodePatch(node_id="node-1", node_type=node_type, op=op, field_path=field_path, new_value=value)
        ],
    )


@pytest.mark.parametrize("node_type", ["agenda", "intent", "clock"])
def test_agenda_intent_clock_nodes_validate(node_type: str) -> None:
    node = _node(node_type)
    assert validate_world_node(node, strict=True).ok
    assert validate_graph_delta(_delta(node_type, "create", node.model_dump()), strict=True).ok


@pytest.mark.parametrize(
    ("node_type", "field_path", "value"),
    [
        ("intent", "profile.action_type", "teleport"),
        ("agenda", "state.priority", 9),
        ("clock", "state.due_at", -1),
        ("character", "state.alive_status", "zombie"),
        ("character", "state.restraint", {"state": "chained"}),
        ("rule", "profile.hardness", "medium"),
        ("agenda", "state.status", "done"),
        ("intent", "state.status", "done"),
        ("intent", "profile.impact_level", "extreme"),
        ("clock", "profile.scope", "private"),
        ("clock", "state.status", "done"),
        ("character", "state.actor_tier", "hero"),
        ("faction", "state.actor_tier", "hero"),
        ("organization", "state.actor_tier", "hero"),
        ("group", "state.actor_tier", "hero"),
        ("agenda", "state.priority", 0),
        ("agenda", "state.priority", True),
        ("agenda", "state.priority", 1.0),
        ("intent", "state.scheduled_at", -1),
        ("intent", "state.scheduled_at", "0"),
        ("intent", "state.slip_count", -1),
        ("intent", "state.slip_count", False),
        ("clock", "state.due_at", None),
        ("clock", "state.due_at", 0.5),
        ("character", "state.next_turn_at", -1),
        ("faction", "state.next_turn_at", True),
        ("organization", "state.next_turn_at", "1"),
        ("group", "state.next_turn_at", None),
        ("intent", "profile.requires_onstage", 1),
        ("character", "state.restraint", None),
        ("character", "state.restraint", {"state": "free"}),
        ("character", "state.restraint", {"state": "free", "by_ref": None, "since": None}),
        ("character", "state.restraint", {"state": "free", "by_ref": "", "since": True}),
        ("character", "state.restraint", {"state": "free", "by_ref": "", "since": "0"}),
        ("rule", "profile.checkable", {}),
        ("rule", "profile.checkable", {"kind": " "}),
        ("rule", "profile.checkable", {"kind": 1}),
        ("rule", "profile.checkable", "condition"),
    ],
)
def test_v6_enum_and_range_errors(node_type: str, field_path: str, value: object) -> None:
    node = _node(node_type)
    section, field = field_path.split(".")
    getattr(node, section)[field] = value
    for report in (
        validate_world_node(node),
        validate_graph_delta(_delta(node_type, "create", node.model_dump())),
        validate_graph_delta(_delta(node_type, "set", value, field_path)),
    ):
        assert not report.ok
        assert any(issue.severity == "error" and field_path in issue.target for issue in report.issues)


@pytest.mark.parametrize(
    ("node_type", "field_path"),
    [
        ("agenda", "profile.owner_ref"),
        ("agenda", "profile.goal"),
        ("intent", "profile.owner_ref"),
        ("intent", "profile.action_type"),
        ("intent", "state.scheduled_at"),
        ("clock", "profile.label"),
        ("clock", "state.due_at"),
    ],
)
def test_v6_required_fields_on_create(node_type: str, field_path: str) -> None:
    node = _node(node_type)
    section, field = field_path.split(".")
    del getattr(node, section)[field]
    for report in (validate_world_node(node), validate_graph_delta(_delta(node_type, "create", node.model_dump()))):
        assert not report.ok
        assert any(
            issue.code == "missing_required_field" and issue.severity == "error" and field_path in issue.target
            for issue in report.issues
        )


def test_set_patch_on_intent_status_is_validated() -> None:
    report = validate_graph_delta(_delta("intent", "set", "done", "state.status"))
    assert report.errors
    assert validate_graph_delta(_delta("intent", "set", "resolved", "state.status")).ok


def test_create_patch_uses_delta_project_id_for_required_field_validation() -> None:
    report = validate_graph_delta(_delta("agenda", "create", {"profile": {"goal": "A goal"}}))
    assert [issue.code for issue in report.errors] == ["missing_required_field"]
    assert report.errors[0].target == "node:node-1:profile.owner_ref"
    assert validate_graph_delta(
        _delta("agenda", "create", {"profile": {"owner_ref": "node:actor", "goal": "A goal"}})
    ).ok


def test_rival_of_is_a_social_edge() -> None:
    edge = WorldEdge(
        id="edge-1", project_id="project-1", source_id="actor-a", target_id="actor-b",
        edge_type="rival_of", edge_family="social",
    )
    assert validate_world_edge(edge).ok


@pytest.mark.parametrize(
    ("node_type", "field_path", "values"),
    [
        ("agenda", "state.status", ("active", "achieved", "abandoned", "failed")),
        ("intent", "state.status", ("planned", "due", "resolved", "disrupted", "vetoed", "held")),
        ("intent", "profile.action_type", (
            "move", "confront", "negotiate", "ally", "betray", "acquire", "transfer", "investigate",
            "spread_info", "deceive", "build", "recruit", "hide", "train", "decree", "other",
        )),
        ("intent", "profile.impact_level", ("minor", "moderate", "major", "critical")),
        ("clock", "profile.scope", ("public", "secret")),
        ("clock", "state.status", ("running", "expired", "resolved")),
        ("character", "state.alive_status", ("alive", "dead", "unknown")),
        ("character", "state.actor_tier", ("major", "minor", "background")),
        ("rule", "profile.hardness", ("hard", "soft")),
        ("agenda", "state.priority", (1, 5)),
        ("intent", "state.scheduled_at", (0, 100)),
        ("intent", "state.slip_count", (0, 100)),
        ("clock", "state.due_at", (0, 100)),
        ("character", "state.next_turn_at", (0, 100)),
        ("intent", "profile.requires_onstage", (True, False)),
        ("rule", "profile.checkable", (None, {"kind": "condition"})),
        ("character", "state.restraint", (
            {"state": "free", "by_ref": "", "since": None},
            {"state": "restrained", "by_ref": "node:actor", "since": 0},
            {"state": "imprisoned", "by_ref": "node:actor", "since": -1},
        )),
    ],
)
def test_v6_valid_values(node_type: str, field_path: str, values: tuple) -> None:
    for value in values:
        node = _node(node_type)
        section, field = field_path.split(".")
        getattr(node, section)[field] = value
        assert validate_world_node(node, strict=True).ok
        assert validate_graph_delta(_delta(node_type, "set", value, field_path), strict=True).ok


@pytest.mark.parametrize("node_type", ["character", "faction", "organization", "group"])
def test_actor_fields_are_canonical(node_type: str) -> None:
    node = _node(node_type)
    node.state.update(actor_tier="minor", next_turn_at=0, resources={})
    if node_type in {"organization", "group"}:
        node.profile["summary"] = "An actor"
        node.state["current_state"] = "active"
    if node_type == "group":
        node.profile["group_type"] = "team"
        node.state["member_ids"] = []
    if node_type == "character":
        node.profile["fear"] = "A fear"
        node.state["travel_mode_ids"] = []
    assert validate_world_node(node, strict=True).ok


def test_rule_statement_is_canonical() -> None:
    node = _node("rule")
    node.profile.update(statement="A rule", hardness="hard", checkable=None)
    assert validate_world_node(node, strict=True).ok


def test_existing_unknown_fields_keep_strictness_behavior() -> None:
    node = _node("character")
    node.state["extension"] = "value"
    assert validate_world_node(node).warnings
    assert validate_world_node(node).ok
    assert not validate_world_node(node, strict=True).ok


@pytest.mark.parametrize(
    ("node_type", "field_path", "value", "ok"),
    [
        ("character", "state.restraint.state", "chained", False),
        ("character", "state.restraint.state", "free", True),
        ("character", "state.restraint.by_ref", 1, False),
        ("character", "state.restraint.by_ref", "node:actor", True),
        ("character", "state.restraint.since", False, False),
        ("character", "state.restraint.since", None, True),
        ("rule", "profile.checkable.kind", "", False),
        ("rule", "profile.checkable.kind", "condition", True),
        ("intent", "state", {"status": "done"}, False),
        ("intent", "state", {"status": "due"}, True),
    ],
)
def test_set_patch_validates_nested_and_section_values(node_type: str, field_path: str, value: object, ok: bool) -> None:
    assert validate_graph_delta(_delta(node_type, "set", value, field_path)).ok is ok
