from __future__ import annotations

from forwin.book_state.adapter import BookStateDeltaAdapter
from forwin.book_state.extraction_contract import (
    BookStateExtractionIssue,
    BookStateExtractionRequest,
    BookStateExtractionResult,
)
from forwin.protocol.book_state import GraphDelta
from forwin.extractor.world_v4 import WorldDeltaExtractor
from forwin.world_v4_review_gate import V4ReviewGate


DEFAULT_BOOK_STATE_LAYERS = {"world", "map", "cognition", "narrative"}
_FILTER_METADATA_KEYS = {"requested_book_state_layers", "filtered_patch_counts"}
_WORLD_CONTEXT_FIELDS = (
    "story_time",
    "operation",
    "target_type",
    "target_id",
    "source_type",
    "source_id",
    "world_line_id",
    "summary",
)


def _normalize_book_state_layers(layers: set[str] | None) -> set[str]:
    if layers is None:
        return set(DEFAULT_BOOK_STATE_LAYERS)
    normalized = {str(layer).strip().lower() for layer in layers if str(layer).strip()}
    unknown = normalized - DEFAULT_BOOK_STATE_LAYERS
    if unknown:
        raise ValueError(
            "unknown BookState extraction layers: " + ", ".join(sorted(unknown))
        )
    return normalized or set(DEFAULT_BOOK_STATE_LAYERS)


def _has_meaningful_graph_delta_content(delta: GraphDelta, layers: set[str]) -> bool:
    if (
        delta.node_patches
        or delta.edge_patches
        or delta.fact_patches
        or delta.map_patches
        or delta.cognition_patches
        or delta.narrative_patches
    ):
        return True
    if "world" not in layers or str(delta.delta_type) != "world_state":
        return False
    if any(str(getattr(delta, field, "") or "").strip() for field in _WORLD_CONTEXT_FIELDS):
        return True
    if delta.evidence_refs:
        return True
    metadata = {
        key: value
        for key, value in dict(delta.metadata).items()
        if key not in _FILTER_METADATA_KEYS
    }
    return bool(metadata)


def _merge_filtered_patch_counts(
    existing: object,
    removed_counts: dict[str, int],
) -> dict[str, int]:
    merged: dict[str, int] = {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            layer = str(key)
            if layer not in DEFAULT_BOOK_STATE_LAYERS:
                continue
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                merged[layer] = count
    for key, value in removed_counts.items():
        if value > 0:
            merged[key] = merged.get(key, 0) + value
    return merged


def _filter_graph_delta_layers(
    graph_deltas: list[GraphDelta],
    layers: set[str],
) -> list[GraphDelta]:
    layers = _normalize_book_state_layers(layers)
    requested = sorted(str(layer) for layer in layers)
    filtered: list[GraphDelta] = []
    for delta in graph_deltas:
        update: dict[str, object] = {}
        counts = {
            "world": (
                len(delta.node_patches)
                + len(delta.edge_patches)
                + len(delta.fact_patches)
            ),
            "map": len(delta.map_patches),
            "cognition": len(delta.cognition_patches),
            "narrative": len(delta.narrative_patches),
        }
        if "world" not in layers:
            update["node_patches"] = []
            update["edge_patches"] = []
            update["fact_patches"] = []
        if "map" not in layers:
            update["map_patches"] = []
        if "cognition" not in layers:
            update["cognition_patches"] = []
        if "narrative" not in layers:
            update["narrative_patches"] = []
        metadata = dict(delta.metadata)
        removed_counts = {
            key: value
            for key, value in counts.items()
            if key not in layers and value > 0
        }
        update["metadata"] = {
            **metadata,
            "requested_book_state_layers": requested,
            "filtered_patch_counts": _merge_filtered_patch_counts(
                metadata.get("filtered_patch_counts"),
                removed_counts,
            ),
        }
        filtered_delta = delta.model_copy(update=update)
        if _has_meaningful_graph_delta_content(filtered_delta, layers):
            filtered.append(filtered_delta)
    return filtered


class BookStateGraphDeltaExtractor:
    """Deterministically extract BookState GraphDelta candidates from writer output.

    The first direct-path slice reuses the existing deterministic world_v4
    extraction rules, then converts the approved result into BookState
    GraphDelta candidates. The orchestrator no longer treats the world_v4
    compiler as the canon success condition.
    """

    def __init__(self, *, layers: set[str] | None = None) -> None:
        self.layers = _normalize_book_state_layers(layers)

    def extract(self, request: BookStateExtractionRequest) -> BookStateExtractionResult:
        writer_output = request.writer_output.model_copy(update={"project_id": request.project_id})
        extracted = WorldDeltaExtractor().extract(
            writer_output,
            chapter_intent=request.chapter_intent,
        )
        gate_verdict = V4ReviewGate().review(
            extracted,
            chapter_intent=request.chapter_intent,
            chapter_body=writer_output.body,
        )
        if not gate_verdict.passed or gate_verdict.approved_changes is None:
            return BookStateExtractionResult(
                project_id=request.project_id,
                chapter_number=request.chapter_number,
                accepted=False,
                compatibility_extracted=extracted,
                compatibility_gate_verdict=gate_verdict,
                issues=[
                    BookStateExtractionIssue(
                        severity="error" if issue.severity == "fail" else issue.severity,
                        code=issue.failure_type,
                        message=issue.message,
                        evidence_refs=list(issue.evidence_refs),
                        metadata={
                            "reviewer": issue.reviewer,
                            "repair_patch": dict(issue.repair_patch),
                        },
                    )
                    for issue in gate_verdict.issues
                ],
                metadata={"extraction_path": "book_state_direct"},
            )

        changes = BookStateDeltaAdapter().from_world_change_set(
            gate_verdict.approved_changes,
            approved_by=["book_state_direct_extractor"],
            review_verdict_id=(
                request.review_verdict_id
                or f"book_state_direct_extract_{request.project_id}_{request.chapter_number}"
            ),
            forced_accept_reason=request.forced_accept_reason,
        )
        graph_deltas = [
            delta.model_copy(
                update={
                    "metadata": {
                        **dict(delta.metadata),
                        "extraction_path": "book_state_direct",
                        "compatibility_source": "world_v4_extractor",
                    }
                }
            )
            for delta in changes.graph_deltas
        ]
        graph_deltas = _filter_graph_delta_layers(graph_deltas, self.layers)
        changes = changes.model_copy(update={"graph_deltas": graph_deltas})
        return BookStateExtractionResult(
            project_id=request.project_id,
            chapter_number=request.chapter_number,
            accepted=True,
            changes=changes,
            compatibility_extracted=extracted,
            compatibility_gate_verdict=gate_verdict,
            metadata={
                "extraction_path": "book_state_direct",
                "graph_delta_count": len(graph_deltas),
            },
        )


__all__ = ["BookStateGraphDeltaExtractor"]
