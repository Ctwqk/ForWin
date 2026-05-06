from __future__ import annotations

from forwin.context.request import ContextDraft, ContextIssue, ContextRequest


class ContextIntegrityGate:
    name = "context_integrity"

    def validate(self, request: ContextRequest, draft: ContextDraft) -> list[ContextIssue]:
        issues: list[ContextIssue] = []
        if draft.data.get("project") is None:
            issues.append(
                ContextIssue(
                    code="context_project_missing",
                    severity="error",
                    message="project must be loaded before building chapter context",
                    provider=self.name,
                )
            )
        return issues
