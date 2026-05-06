from __future__ import annotations

from forwin.context.request import ContextDraft, ContextIssue, ContextRequest
from forwin.personality import CharacterPersonalityLibrary


class PersonalityIntegrityGate:
    name = "personality_integrity"

    def validate(self, request: ContextRequest, draft: ContextDraft) -> list[ContextIssue]:
        from forwin.context.assembler import (
            _personality_integrity_issues,
            _project_personality_integrity_strict,
            _save_personality_integrity_failure,
        )

        library = draft.data.setdefault("personality_library", CharacterPersonalityLibrary())
        raw_issues = _personality_integrity_issues(
            book_state_overlay=draft.data.get("book_state_overlay", {}),
            allowed_entities=draft.data.get("allowed_entities", []),
            active_entities=draft.data.get("entities", []),
            library=library,
        )
        draft.data["personality_integrity_issues"] = raw_issues
        issues = [
            ContextIssue(
                code=str(item.get("code") or "personality_integrity_issue"),
                severity=str(item.get("severity") or "error"),
                message=str(item.get("message") or item.get("code") or "personality integrity issue"),
                provider=self.name,
                payload=dict(item),
            )
            for item in raw_issues
            if isinstance(item, dict)
        ]
        project = draft.data.get("project")
        if raw_issues and project is not None and _project_personality_integrity_strict(project):
            _save_personality_integrity_failure(
                request.session,
                request.project_id,
                request.chapter_plan.chapter_number,
                raw_issues,
            )
            error_codes = ", ".join(
                str(item.get("code") or "")
                for item in raw_issues
                if str(item.get("severity") or "") == "error"
            )
            if error_codes:
                raise ValueError(error_codes)
        return issues
