from __future__ import annotations

from forwin.book_state.adapter import BookStateDeltaAdapter
from forwin.book_state.extraction_contract import (
    BookStateExtractionIssue,
    BookStateExtractionRequest,
    BookStateExtractionResult,
)
from forwin.extractor.world_v4 import WorldDeltaExtractor
from forwin.world_v4_review_gate import V4ReviewGate


class BookStateGraphDeltaExtractor:
    """Deterministically extract BookState GraphDelta candidates from writer output.

    The first direct-path slice reuses the existing deterministic world_v4
    extraction rules, then converts the approved result into BookState
    GraphDelta candidates. The orchestrator no longer treats the world_v4
    compiler as the canon success condition.
    """

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
