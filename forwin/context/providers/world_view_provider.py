from __future__ import annotations

import logging

from forwin.context.request import ContextDraft, ContextRequest

logger = logging.getLogger(__name__)


class WorldViewContextProvider:
    """The chapter's POV world view and the beat-sheet phase it falls in."""

    name = "world_view"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        from forwin.world_view.builder import build_chapter_world_view

        session = request.session
        if session is None:
            return
        chapter_plan = request.chapter_plan
        draft.data["world_view"] = build_chapter_world_view(
            session,
            project_id=request.project_id,
            chapter_plan=chapter_plan,
            budget_chars=_budget_chars(session, request.project_id),
            # The BookState provider's runtime: one load per assembly.
            runtime=draft.data.get("book_state_runtime"),
        )
        draft.data["arc_phase_purpose"] = _phase_purpose(session, chapter_plan)


def _budget_chars(session, project_id: str) -> int:
    from forwin.models.project import Project
    from forwin.runtime.policy import WriterPolicy
    from forwin.runtime.policy_store import ProjectPolicyMissing, ProjectPolicyStore

    project = session.get(Project, project_id)
    if project is not None:
        try:
            return ProjectPolicyStore(session).load(project).policy.writer.world_view_budget_chars
        except ProjectPolicyMissing:
            pass
    logger.debug("Project %s has no runtime policy; the world view uses the standard budget", project_id)
    return WriterPolicy().world_view_budget_chars


def _phase_purpose(session, chapter_plan) -> str:
    """The purpose of the active beat-sheet phase that covers the chapter; empty without one."""
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository

    repository = ArcBeatSheetRepository(session)
    row = repository.active_for_arc(str(getattr(chapter_plan, "arc_plan_id", "") or ""))
    if row is None:
        return ""
    chapter_number = int(chapter_plan.chapter_number)
    return next(
        (
            phase.purpose for phase in repository.load(row).phases
            if phase.chapter_range[0] <= chapter_number <= phase.chapter_range[1]
        ),
        "",
    )
