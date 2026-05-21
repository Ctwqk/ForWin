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
    legacy_compatibility: dict[str, object] = Field(default_factory=dict)


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
            is_legacy_location = patch.field_path == "state.location"
            issues.append(
                BookStateReviewIssue(
                    severity="warning" if is_legacy_location else "error",
                    code="movement_unknown_map_node",
                    target_ref=f"node:{patch.node_id}",
                    message=f"movement references unknown map node: {old_location} -> {new_location}",
                    legacy_compatibility=(
                        {
                            "compat_layer": "book_state",
                            "compat_feature": "book_state.state.location_patch_warning",
                            "usage_kind": "read_fallback",
                            "source_module": "forwin.book_state.reviewer",
                            "usage_reason": "legacy state.location patch downgraded to warning",
                            "compat_key": "state.location",
                            "legacy_identifier": f"{old_location}->{new_location}",
                            "canonical_identifier": patch.node_id,
                            "related_stage": "book_state_review",
                        }
                        if is_legacy_location
                        else {}
                    ),
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
    return str(patch.op) in {"set", "replace"} and patch.field_path in {"state.location_id", "state.location"}


__all__ = ["BookStateReviewGate", "BookStateReviewIssue", "BookStateReviewVerdict"]
