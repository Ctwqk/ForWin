from __future__ import annotations

from forwin.orchestrator_loop_core.common import *
from forwin.subworld.admission_patch import apply_subworld_admission_patch


def _apply_subworld_admission_repair_patch(
    self,
    *,
    session: Session,
    repo: StateRepository,
    project_id: str,
    chapter_plan: ChapterPlan,
    context,
    current_output: WriterOutput,
    current_plan: ChapterExperiencePlan,
    band_schedule: BandDelightSchedule | None,
    patch: dict[str, object],
    repair_instruction: RepairInstruction,
) -> tuple[dict[str, object], Any, dict[str, object], dict[str, object], str]:
    active_subworld_ids = list(current_plan.active_subworld_ids)
    if not active_subworld_ids and band_schedule is not None:
        active_subworld_ids = list(band_schedule.active_subworld_ids)
    patch_result = apply_subworld_admission_patch(
        session=session,
        project_id=project_id,
        chapter_plan=chapter_plan,
        writer_output=current_output,
        repair_instruction=repair_instruction,
        current_plan=current_plan,
        active_subworld_ids=active_subworld_ids,
        context=context,
        protected_names=self._project_character_names(repo, project_id),
    )
    design_patch = {
        **patch,
        **patch_result.design_patch,
        "subworld_admission_patch_skip_writer": not patch_result.requires_writer_rewrite,
    }
    updated_context = context.model_copy(
        update={"chapter_experience_plan": patch_result.updated_plan}
    )
    if not patch_result.failure_reason:
        session.add(chapter_plan)
        session.flush()
    return (
        patch_result.updated_plan.model_dump(mode="json"),
        updated_context,
        self._chapter_plan_snapshot(
            repo=repo,
            project_id=project_id,
            chapter_plan=chapter_plan,
            experience_plan=patch_result.updated_plan,
        ),
        self._band_plan_snapshot(
            repo=repo,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            schedule=band_schedule,
        ),
        patch_result.failure_reason,
    )


__all__ = ["_apply_subworld_admission_repair_patch"]
