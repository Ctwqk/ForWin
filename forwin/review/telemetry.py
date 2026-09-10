"""Review-specific events share the transaction-bound recorder with Writer and Canon."""

from __future__ import annotations

from dataclasses import dataclass

from forwin.audit.events import DecisionEventType
from forwin.observability.payloads import audit_payload
from forwin.observability.pipeline_trace import PipelineTraceRecorder
from forwin.protocol.review import ReviewVerdict
from forwin.state.updater import StateUpdater


@dataclass(frozen=True, slots=True)
class ReviewTelemetry:
    recorder: PipelineTraceRecorder

    def record_map_issues(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        review: ReviewVerdict,
        parent_event_id: str = "",
    ) -> None:
        issues = [
            issue
            for issue in review.issues
            if str(getattr(issue, "rule_name", "") or "").startswith("map_")
        ]
        if not issues:
            return
        self.recorder.record_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.MAP_MOVEMENT_REVIEW_ISSUE,
            scope="chapter",
            summary=f"第{chapter_number}章 map movement reviewer 发现 {len(issues)} 个问题。",
            payload=audit_payload(
                stage="map_movement_review",
                status="issue",
                operation_id=self.recorder.audit.operation_id,
                issue_count=len(issues),
                issues=[
                    {
                        "rule_name": str(issue.rule_name or ""),
                        "issue_type": str(issue.issue_type or ""),
                        "severity": str(issue.severity or ""),
                        "issue_group": str(issue.issue_group or ""),
                        "target_scope": str(issue.target_scope or ""),
                        "entity_names": list(issue.entity_names or []),
                        "evidence_refs": list(issue.evidence_refs or []),
                    }
                    for issue in issues
                ],
            ),
            parent_event_id=parent_event_id,
        )
