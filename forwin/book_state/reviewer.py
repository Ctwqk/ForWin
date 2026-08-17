from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.book_state.projection import BookStateProjection
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import validate_graph_delta
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch


class BookStateReviewIssue(BaseModel):
    severity: str
    code: str
    target_ref: str
    message: str


class BookStateReviewVerdict(BaseModel):
    project_id: str
    chapter_number: int
    verdict_id: str
    accepted: bool
    issues: list[BookStateReviewIssue] = Field(default_factory=list)
    approved_changes: ApprovedGraphDeltaSet | None = None


class BookStateReviewGate:
    """Deterministic guardrail for BookState graph patches before commit."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.projection = BookStateProjection(session)

    def review(
        self,
        changes: ApprovedGraphDeltaSet,
        *,
        base_chapter: int | None = None,
    ) -> BookStateReviewVerdict:
        runtime = self.projection.load_runtime_as_of(
            changes.project_id,
            as_of_chapter=base_chapter if base_chapter is not None else max(changes.chapter_number - 1, 0),
        )
        issues: list[BookStateReviewIssue] = []
        for delta in changes.graph_deltas:
            issues.extend(_canon_permission_issues(delta))
            issues.extend(_schema_issues(delta))
            issues.extend(_writer_hidden_truth_issues(delta))
            issues.extend(_immutable_rule_definition_issues(runtime, delta))
            issues.extend(_movement_issues(runtime, delta))
        accepted = not any(issue.severity == "error" for issue in issues)
        return BookStateReviewVerdict(
            project_id=changes.project_id,
            chapter_number=changes.chapter_number,
            verdict_id=f"book_state_review_{changes.project_id}_{changes.chapter_number}",
            accepted=accepted,
            issues=issues,
            approved_changes=changes if accepted else None,
        )


def _schema_issues(delta: GraphDelta) -> list[BookStateReviewIssue]:
    report = validate_graph_delta(delta)
    return [
        BookStateReviewIssue(
            severity=issue.severity,
            code=issue.code,
            target_ref=issue.target,
            message=issue.message,
        )
        for issue in report.issues
    ]


def _canon_permission_issues(delta: GraphDelta) -> list[BookStateReviewIssue]:
    if delta.allowed_for_canon:
        return []
    return [
        BookStateReviewIssue(
            severity="error",
            code="delta_not_allowed_for_canon",
            target_ref=f"delta:{delta.id}",
            message="GraphDelta is not approved for BookState canon.",
        )
    ]


def _writer_hidden_truth_issues(delta: GraphDelta) -> list[BookStateReviewIssue]:
    role = str(delta.metadata.get("role") or delta.metadata.get("pack_role") or delta.source_type or "").lower()
    if role not in {"writer", "writing", "writer_pack"}:
        return []
    issues: list[BookStateReviewIssue] = []
    for patch in delta.fact_patches:
        hidden = patch.sensitivity_level in {"hidden", "secret", "must_not_reveal"}
        payload_hidden = isinstance(patch.new_value, dict) and str(
            patch.new_value.get("sensitivity_level", "")
        ) in {"hidden", "secret", "must_not_reveal"}
        if hidden or payload_hidden:
            issues.append(
                BookStateReviewIssue(
                    severity="error",
                    code="writer_hidden_truth_leak",
                    target_ref=f"fact:{patch.fact_id}",
                    message="writer-scoped deltas cannot introduce hidden objective truth.",
                )
            )
    for patch in delta.node_patches:
        visibility = str(getattr(patch, "visibility_default", "") or "")
        payload = patch.new_value if isinstance(patch.new_value, dict) else {}
        status = str(payload.get("status", "") or "")
        tags = {str(tag) for tag in payload.get("tags", [])} if isinstance(payload.get("tags"), list) else set()
        if visibility == "hidden" or status in {"hidden", "secret"} or tags.intersection({"hidden", "secret", "must_not_reveal"}):
            issues.append(
                BookStateReviewIssue(
                    severity="error",
                    code="writer_hidden_truth_leak",
                    target_ref=f"node:{patch.node_id}",
                    message="writer-scoped deltas cannot introduce hidden canon nodes.",
                )
            )
    return issues


def _immutable_rule_definition_issues(
    runtime: BookStateRuntime,
    delta: GraphDelta,
) -> list[BookStateReviewIssue]:
    if not _is_writer_delta(delta) or _has_explicit_rule_bridge(delta):
        return []
    issues: list[BookStateReviewIssue] = []
    for patch in delta.node_patches:
        node = runtime.world.nodes_by_id.get(patch.node_id)
        if node is None or str(node.node_type or "") != "rule":
            continue
        field_path = str(patch.field_path or "").strip()
        if not _is_immutable_rule_definition_path(field_path):
            continue
        current = _world_node_field_value(runtime, node.id, field_path)
        if _same_value(current, patch.new_value):
            continue
        issues.append(
            BookStateReviewIssue(
                severity="error",
                code="immutable_rule_definition_conflict",
                target_ref=f"node:{node.id}:{field_path or 'definition'}",
                message=(
                    f"writer delta cannot rewrite canonical rule definition {node.name or node.id} "
                    f"at {field_path or 'definition'} without an explicit canon bridge"
                ),
            )
        )
    return issues


def _is_writer_delta(delta: GraphDelta) -> bool:
    return (
        str(delta.source_type or "") in {"writer", "writer_output", "writing"}
        or str(delta.operation or "") == "apply_writer_contract"
        or str(delta.metadata.get("extraction_path") or "") == "writer_contract"
    )


def _has_explicit_rule_bridge(delta: GraphDelta) -> bool:
    return bool(delta.metadata.get("explicit_rule_definition_bridge")) and str(
        delta.source_type or ""
    ) in {"operator", "canon_repair", "retcon"}


def _is_immutable_rule_definition_path(field_path: str) -> bool:
    return (
        field_path in {"name", "aliases", "summary", "description", "profile"}
        or field_path.startswith(("profile.", "metadata.writer_state."))
    )


def _world_node_field_value(
    runtime: BookStateRuntime,
    node_id: str,
    field_path: str,
):
    node = runtime.world.nodes_by_id.get(node_id)
    if node is None:
        return None
    value = node.model_dump(mode="python")
    for part in field_path.split(".") if field_path else []:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _same_value(left, right) -> bool:
    return left == right


def _movement_issues(runtime: BookStateRuntime, delta: GraphDelta) -> list[BookStateReviewIssue]:
    issues: list[BookStateReviewIssue] = []
    for patch in delta.node_patches:
        if not _is_location_patch(patch):
            continue
        old_location = str(patch.old_value or "")
        new_location = str(patch.new_value or "")
        if not old_location or not new_location or old_location == new_location:
            continue
        if old_location not in runtime.map.nodes_by_id or new_location not in runtime.map.nodes_by_id:
            issues.append(
                BookStateReviewIssue(
                    severity="error",
                    code="movement_unknown_map_node",
                    target_ref=f"node:{patch.node_id}",
                    message=f"movement references unknown map node: {old_location} -> {new_location}",
                )
            )
            continue
        result = runtime.map.shortest_path(old_location, new_location, metric="travel_time")
        if not result.reachable:
            issues.append(
                BookStateReviewIssue(
                    severity="error",
                    code="movement_unreachable",
                    target_ref=f"node:{patch.node_id}",
                    message=f"no objective map path for movement {old_location} -> {new_location}: {result.blocked_reason}",
                )
            )
    return issues


def _is_location_patch(patch: NodePatch) -> bool:
    return str(patch.op) in {"set", "replace"} and patch.field_path == "state.location_id"


__all__ = ["BookStateReviewGate", "BookStateReviewIssue", "BookStateReviewVerdict"]
