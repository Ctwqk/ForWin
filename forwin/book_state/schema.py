from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from forwin.protocol.book_state import (
    FactNode,
    GraphDelta,
    MapEdge,
    MapNode,
    WORLD_EDGE_TYPES_BY_FAMILY,
    WorldEdge,
    WorldNode,
)


WORLD_NODE_FIELDS: dict[str, dict[str, set[str]]] = {
    "character": {
        "profile": {
            "gender",
            "species",
            "age_bracket",
            "culture_tag",
            "origin_faction_id",
            "role_archetype",
            "public_identity",
            "true_identity_ref",
            "personality_tags",
            "personality_loadout",
            "talent_profile",
            "narrative_role",
            "first_appearance_chapter",
        },
        "state": {
            "location_id",
            "status",
            "health",
            "mood",
            "goal",
            "long_term_goal",
            "current_activity_id",
            "faction_id",
            "rank_title",
            "power_level",
            "ability_ids",
            "item_ids",
            "equipped_item_ids",
            "resource_summary",
            "relationship_summary",
            "secret_flags",
            "reputation",
            "promise_debt_refs",
            "last_seen_chapter",
            "state_summary",
        },
    },
    "faction": {
        "profile": {
            "faction_type",
            "culture_tag",
            "founding_story",
            "ideology",
            "public_goal",
            "hidden_goal",
            "hierarchy_model",
            "base_location_id",
            "core_member_ids",
            "resource_types",
            "narrative_role",
        },
        "state": {
            "status",
            "headquarters_location_id",
            "controlled_site_ids",
            "leader_id",
            "current_goal",
            "active_plan_ids",
            "power_level",
            "military_strength",
            "wealth_level",
            "influence_level",
            "reputation",
            "alliance_ids",
            "enemy_ids",
            "resource_summary",
            "pressure_summary",
            "secret_flags",
            "state_summary",
        },
    },
    "group": {
        "profile": {
            "group_type",
            "formation_reason",
            "member_rule",
            "public_label",
            "hidden_identity",
            "culture_tag",
            "narrative_role",
            "default_location_id",
            "initial_member_ids",
        },
        "state": {
            "status",
            "location_id",
            "member_ids",
            "leader_id",
            "current_goal",
            "morale",
            "cohesion",
            "resource_summary",
            "visibility_level",
            "activity_id",
            "pressure_summary",
            "state_summary",
        },
    },
    "item": {
        "profile": {
            "item_type",
            "rarity",
            "origin",
            "creator_id",
            "material_tags",
            "required_level",
            "binding_rule_id",
            "ability_ids",
            "limitation_rule_ids",
            "appearance",
            "public_legend",
            "true_origin_fact_id",
            "narrative_role",
        },
        "state": {
            "status",
            "owner_id",
            "holder_id",
            "equipped_by_id",
            "location_id",
            "durability",
            "seal_status",
            "awakened_level",
            "charge_level",
            "known_effect_refs",
            "hidden_effect_refs",
            "current_form",
            "lock_status",
            "contamination_state",
            "visibility_level",
            "last_used_chapter",
            "state_summary",
        },
    },
    "resource": {
        "profile": {
            "resource_type",
            "unit",
            "stackable",
            "natural_source",
            "economic_role",
            "strategic_role",
            "rarity",
            "storage_rule",
            "decay_rule_id",
        },
        "state": {
            "quantity",
            "owner_id",
            "holder_id",
            "location_id",
            "controlled_by_id",
            "flow_rate",
            "quality_level",
            "reserved_amount",
            "consumed_amount",
            "scarcity_level",
            "visibility_level",
            "state_summary",
        },
    },
    "ability": {
        "profile": {
            "ability_type",
            "system_path",
            "level_scale",
            "element_tags",
            "acquisition_method",
            "cost_rule_id",
            "cooldown_rule_id",
            "counter_tags",
            "upgrade_path",
            "forbidden_rule_ids",
            "public_description",
            "true_mechanism_fact_id",
        },
        "state": {
            "owner_id",
            "mastery_level",
            "active_status",
            "cooldown_state",
            "cost_state",
            "mutation_state",
            "known_by_refs",
            "hidden_mechanism_refs",
            "last_used_chapter",
            "risk_level",
            "limitation_state",
            "state_summary",
        },
    },
    "rule": {
        "profile": {
            "rule_type",
            "scope",
            "authority_source",
            "applies_to_types",
            "trigger_condition",
            "effect_description",
            "exception_refs",
            "penalty_refs",
            "related_ability_ids",
            "related_item_ids",
            "public_version",
            "hidden_version_fact_id",
        },
        "state": {
            "status",
            "enforcement_level",
            "discovered_by_refs",
            "broken_by_refs",
            "loophole_refs",
            "stability_level",
            "last_triggered_chapter",
            "visibility_level",
            "state_summary",
        },
    },
    "activity": {
        "profile": {
            "activity_type",
            "organizer_id",
            "host_location_id",
            "announced_story_time",
            "planned_start_time",
            "planned_end_time",
            "entry_rules",
            "reward_refs",
            "participant_limit",
            "public_purpose",
            "secret_purpose_fact_id",
            "judging_rule_id",
            "narrative_role",
            "related_world_line_id",
        },
        "state": {
            "status",
            "current_phase",
            "participant_ids",
            "eliminated_ids",
            "ranking",
            "active_matchups",
            "active_task_refs",
            "security_level",
            "audience_pressure",
            "faction_interference_refs",
            "reward_status",
            "scandal_refs",
            "accident_refs",
            "current_location_id",
            "visibility_level",
            "last_phase_chapter",
            "result_summary",
            "state_summary",
        },
    },
    "site_state": {
        "profile": {
            "map_node_id",
            "site_type",
            "origin_culture",
            "public_description",
            "hidden_origin_fact_id",
            "entry_rule_id",
            "layered_structure_refs",
            "default_danger_level",
            "reward_refs",
            "trap_refs",
            "narrative_role",
        },
        "state": {
            "status",
            "controller_faction_id",
            "occupant_ids",
            "exploration_progress",
            "opened_by_id",
            "seal_status",
            "active_traps",
            "hidden_zone_ids",
            "discovered_zone_ids",
            "remaining_reward_refs",
            "extracted_reward_refs",
            "boss_state_refs",
            "corruption_level",
            "danger_level",
            "access_status",
            "visibility_level",
            "last_changed_chapter",
            "state_summary",
        },
    },
    "event": {
        "profile": {
            "event_type",
            "related_world_line_id",
            "planned_or_actual",
            "expected_chapter",
            "expected_story_time",
            "involved_node_refs",
            "location_id",
            "narrative_function",
            "public_summary",
            "hidden_truth_fact_id",
        },
        "state": {
            "status",
            "actual_chapter",
            "actual_story_time",
            "participant_ids",
            "witness_ids",
            "result_refs",
            "consequence_refs",
            "visibility_level",
            "evidence_refs",
            "disputed_by_refs",
            "retcon_status",
            "state_summary",
        },
    },
    "fact": {
        "profile": {
            "proposition",
            "fact_type",
            "truth_value",
            "confidence",
            "related_node_refs",
            "related_edge_refs",
            "source_refs",
            "created_at_chapter",
            "happened_at_story_time",
            "contradiction_refs",
            "sensitivity_level",
            "narrative_function",
        },
        "state": {
            "status",
            "public_visibility",
            "reader_visibility",
            "confirmed_by_refs",
            "denied_by_refs",
            "outdated_by_fact_id",
            "reveal_plan_id",
            "last_updated_chapter",
            "canonicality_level",
            "state_summary",
        },
    },
    "objective": {
        "profile": {
            "objective_type",
            "owner_id",
            "target_refs",
            "success_condition",
            "failure_condition",
            "public_goal",
            "hidden_goal_fact_id",
            "priority",
            "deadline_story_time",
            "narrative_role",
            "related_world_line_id",
        },
        "state": {
            "status",
            "progress",
            "current_phase",
            "blocker_refs",
            "enabler_refs",
            "resource_refs",
            "participant_ids",
            "opponent_ids",
            "risk_level",
            "visibility_level",
            "last_progress_chapter",
            "result_summary",
            "state_summary",
        },
    },
}

_V46_GENERIC_PROFILE_FIELDS = {
    "summary",
    "scope",
    "status",
    "source_refs",
    "visibility_level",
    "narrative_role",
    "metadata",
}
_V46_GENERIC_STATE_FIELDS = {
    "status",
    "current_state",
    "state_summary",
    "visibility_level",
    "last_updated_chapter",
    "source_refs",
    "metadata",
}
for _node_type in (
    "organization",
    "family",
    "subworld",
    "region",
    "location",
    "institution",
    "technology",
    "magic_system",
    "thread",
    "secret",
    "knowledge_gap",
    "reader_promise",
    "conflict",
    "contract",
):
    WORLD_NODE_FIELDS.setdefault(
        _node_type,
        {
            "profile": set(_V46_GENERIC_PROFILE_FIELDS),
            "state": set(_V46_GENERIC_STATE_FIELDS),
        },
    )

COGNITION_REF_PREFIXES = {
    "node:",
    "field:",
    "edge:",
    "fact:",
    "map_node:",
    "map_edge:",
    "world_line:",
    "promise:",
    "knowledge_gap:",
}


@dataclass(frozen=True)
class BookStateSchemaIssue:
    severity: str
    code: str
    target: str
    message: str


@dataclass(frozen=True)
class BookStateSchemaReport:
    issues: tuple[BookStateSchemaIssue, ...]

    @property
    def errors(self) -> list[BookStateSchemaIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[BookStateSchemaIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_world_node(node: WorldNode, *, strict: bool = False) -> BookStateSchemaReport:
    issues: list[BookStateSchemaIssue] = []
    node_type = str(node.node_type)
    allowed = WORLD_NODE_FIELDS.get(node_type)
    if allowed is None:
        issues.append(_issue("error", "unknown_world_node_type", node.id, f"unknown world node type: {node_type}"))
        return BookStateSchemaReport(tuple(issues))

    issues.extend(_unknown_field_issues(node.id, "profile", node.profile, allowed["profile"], strict=strict))
    issues.extend(_unknown_field_issues(node.id, "state", node.state, allowed["state"], strict=strict))
    return BookStateSchemaReport(tuple(issues))


def validate_world_edge(edge: WorldEdge) -> BookStateSchemaReport:
    issues: list[BookStateSchemaIssue] = []
    family = str(edge.edge_family)
    if family not in WORLD_EDGE_TYPES_BY_FAMILY:
        issues.append(_issue("error", "unknown_world_edge_family", edge.id, f"unknown world edge family: {family}"))
    elif edge.edge_type not in WORLD_EDGE_TYPES_BY_FAMILY[family]:
        issues.append(
            _issue(
                "error",
                "world_edge_family_mismatch",
                edge.id,
                f"edge_type {edge.edge_type!r} does not belong to {family!r}",
            )
        )
    if not edge.source_id or not edge.target_id:
        issues.append(_issue("error", "world_edge_missing_endpoint", edge.id, "world edge requires source_id and target_id"))
    return BookStateSchemaReport(tuple(issues))


def validate_graph_delta(delta: GraphDelta, *, strict: bool = False) -> BookStateSchemaReport:
    issues: list[BookStateSchemaIssue] = []
    for patch in delta.node_patches:
        node_type = str(patch.node_type)
        if node_type not in WORLD_NODE_FIELDS:
            issues.append(
                _issue("error", "unknown_node_patch_type", f"node:{patch.node_id}", f"unknown node patch type: {node_type}")
            )
            continue
        if str(patch.op) == "create":
            payload = patch.new_value if isinstance(patch.new_value, dict) else {}
            try:
                node = WorldNode.model_validate({"id": patch.node_id, "node_type": node_type, **payload})
            except Exception as exc:  # noqa: BLE001 - validation details belong in the report.
                issues.append(_issue("error", "invalid_node_create_patch", f"node:{patch.node_id}", str(exc)))
                continue
            issues.extend(validate_world_node(node, strict=strict).issues)
        elif patch.field_path:
            issues.extend(_validate_node_patch_field(patch.node_id, node_type, patch.field_path, strict=strict))

    for patch in delta.edge_patches:
        if str(patch.op) == "create":
            payload = patch.new_value if isinstance(patch.new_value, dict) else {}
            try:
                edge = WorldEdge.model_validate(
                    {
                        "id": patch.edge_id,
                        "project_id": delta.project_id,
                        "source_id": patch.source_id or payload.get("source_id", ""),
                        "target_id": patch.target_id or payload.get("target_id", ""),
                        "edge_type": patch.edge_type or payload.get("edge_type", ""),
                        "edge_family": patch.edge_family or payload.get("edge_family", ""),
                        **payload,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                issues.append(_issue("error", "invalid_edge_create_patch", f"edge:{patch.edge_id}", str(exc)))
                continue
            issues.extend(validate_world_edge(edge).issues)

    for patch in delta.fact_patches:
        if str(patch.op) == "create":
            payload = patch.new_value if isinstance(patch.new_value, dict) else {}
            try:
                FactNode.model_validate(
                    {
                        "id": patch.fact_id,
                        "project_id": delta.project_id,
                        "proposition": patch.proposition or payload.get("proposition", ""),
                        "truth_value": patch.truth_value or payload.get("truth_value", "true"),
                        **payload,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                issues.append(_issue("error", "invalid_fact_create_patch", f"fact:{patch.fact_id}", str(exc)))

    for patch in delta.map_patches:
        if str(patch.op) != "create":
            if patch.target_type not in {"map_node", "map_edge"}:
                issues.append(_issue("error", "invalid_map_patch_target", patch.target_id, f"invalid target_type: {patch.target_type}"))
            continue
        payload = patch.new_value if isinstance(patch.new_value, dict) else {}
        try:
            if patch.target_type == "map_node":
                MapNode.model_validate(payload)
            elif patch.target_type == "map_edge":
                MapEdge.model_validate(payload)
            else:
                issues.append(_issue("error", "invalid_map_patch_target", patch.target_id, f"invalid target_type: {patch.target_type}"))
        except Exception as exc:  # noqa: BLE001
            issues.append(_issue("error", "invalid_map_create_patch", patch.target_id, str(exc)))

    for patch in delta.cognition_patches:
        if patch.field_path.endswith("_refs") and patch.new_value is not None:
            for ref in _iter_ref_values(patch.new_value):
                if not _valid_cognition_ref(ref):
                    issues.append(_issue("warning", "noncanonical_cognition_ref", ref, f"noncanonical cognition ref: {ref}"))

    return BookStateSchemaReport(tuple(issues))


def validate_runtime(world_nodes: Iterable[WorldNode], world_edges: Iterable[WorldEdge]) -> BookStateSchemaReport:
    issues: list[BookStateSchemaIssue] = []
    node_ids = {node.id for node in world_nodes}
    for node in world_nodes:
        issues.extend(validate_world_node(node).issues)
    for edge in world_edges:
        issues.extend(validate_world_edge(edge).issues)
        if edge.source_id not in node_ids:
            issues.append(_issue("warning", "world_edge_unknown_source", f"edge:{edge.id}", f"unknown source_id: {edge.source_id}"))
        if edge.target_id not in node_ids:
            issues.append(_issue("warning", "world_edge_unknown_target", f"edge:{edge.id}", f"unknown target_id: {edge.target_id}"))
    return BookStateSchemaReport(tuple(issues))


def _validate_node_patch_field(
    node_id: str,
    node_type: str,
    field_path: str,
    *,
    strict: bool,
) -> list[BookStateSchemaIssue]:
    if not field_path.startswith(("profile.", "state.")):
        return []
    section, field_name = field_path.split(".", 1)
    field_name = field_name.split(".", 1)[0]
    allowed = WORLD_NODE_FIELDS[node_type][section]
    if field_name in allowed:
        return []
    severity = "error" if strict else "warning"
    return [
        _issue(
            severity,
            "unknown_node_patch_field",
            f"node:{node_id}:{field_path}",
            f"{field_path!r} is not a canonical {node_type} field",
        )
    ]


def _unknown_field_issues(
    node_id: str,
    section: str,
    payload: dict[str, Any],
    allowed: set[str],
    *,
    strict: bool,
) -> list[BookStateSchemaIssue]:
    severity = "error" if strict else "warning"
    return [
        _issue(
            severity,
            "unknown_node_field",
            f"node:{node_id}:{section}.{field_name}",
            f"{section}.{field_name} is not a canonical field",
        )
        for field_name in sorted(set(payload) - allowed)
    ]


def _valid_cognition_ref(ref: str) -> bool:
    return any(ref.startswith(prefix) for prefix in COGNITION_REF_PREFIXES)


def _iter_ref_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list | tuple | set):
        for item in value:
            if isinstance(item, str):
                yield item


def _issue(severity: str, code: str, target: str, message: str) -> BookStateSchemaIssue:
    return BookStateSchemaIssue(severity=severity, code=code, target=target, message=message)


__all__ = [
    "BookStateSchemaIssue",
    "BookStateSchemaReport",
    "COGNITION_REF_PREFIXES",
    "WORLD_NODE_FIELDS",
    "validate_graph_delta",
    "validate_runtime",
    "validate_world_edge",
    "validate_world_node",
]
