from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from forwin.api_schema import (
    BandCheckpointApproveRequest,
    BandExperienceOverrideRequest,
    ManualCheckpointRequest,
    NarrativeConstraintCreateRequest,
    NarrativeConstraintUpdateRequest,
    ScenarioPlanPatchApproveRequest,
    TaskContractUpdateRequest,
    TropeTemplateValidationRequest,
)

from . import operations


@dataclass(frozen=True, slots=True)
class ProjectControlApplicationDeps:
    get_session: Callable[[], Any]
    get_pipeline: Callable[[], Any]
    display_datetime: Callable[[Any], str]
    require_reason: Callable[..., str]
    validate_constraint_payload: Callable[..., tuple[str, str, str]]
    serialize_band_checkpoint: Callable[..., Any]
    serialize_constraint: Callable[..., Any]
    list_decision_event_rows: Callable[..., list[Any]]
    serialize_decision_event: Callable[[Any], Any]
    build_causal_replay: Callable[..., Any]
    build_audit_insights: Callable[..., Any]
    latest_band_checkpoint_row: Callable[..., Any]
    latest_related_decision_event: Callable[..., Any]
    log_decision_event: Callable[..., Any]
    json_load_object: Callable[[str | None], dict[str, Any]]


def _build_operations(
    deps: ProjectControlApplicationDeps,
) -> dict[str, Callable[..., Any]]:
    get_session = deps.get_session
    get_pipeline = deps.get_pipeline
    display_datetime = deps.display_datetime
    require_reason = deps.require_reason
    validate_constraint_payload = deps.validate_constraint_payload
    serialize_band_checkpoint = deps.serialize_band_checkpoint
    serialize_constraint = deps.serialize_constraint
    list_decision_event_rows = deps.list_decision_event_rows
    serialize_decision_event = deps.serialize_decision_event
    build_causal_replay = deps.build_causal_replay
    build_audit_insights = deps.build_audit_insights
    latest_band_checkpoint_row = deps.latest_band_checkpoint_row
    latest_related_decision_event = deps.latest_related_decision_event
    log_decision_event = deps.log_decision_event
    json_load_object = deps.json_load_object
    def create_manual_checkpoint(project_id: str, req: ManualCheckpointRequest):
        return operations.create_manual_checkpoint(
            project_id,
            req,
            get_session=get_session,
            require_reason=require_reason,
            serialize_band_checkpoint=serialize_band_checkpoint,
            log_decision_event=log_decision_event,
        )

    def get_band_checkpoint(project_id: str, band_id: str):
        return operations.get_band_checkpoint(
            project_id,
            band_id,
            get_session=get_session,
            latest_band_checkpoint_row=latest_band_checkpoint_row,
            serialize_band_checkpoint=serialize_band_checkpoint,
        )

    def approve_band_checkpoint(
        project_id: str, band_id: str, req: BandCheckpointApproveRequest
    ):
        return operations.approve_band_checkpoint(
            project_id,
            band_id,
            req,
            get_session=get_session,
            latest_band_checkpoint_row=latest_band_checkpoint_row,
            latest_related_decision_event=latest_related_decision_event,
            require_reason=require_reason,
            log_decision_event=log_decision_event,
            serialize_band_checkpoint=serialize_band_checkpoint,
        )

    def get_chapter_task_contract(project_id: str, chapter_number: int):
        return operations.get_chapter_task_contract(
            project_id,
            chapter_number,
            get_session=get_session,
        )

    def update_chapter_task_contract(
        project_id: str,
        chapter_number: int,
        req: TaskContractUpdateRequest,
    ):
        return operations.update_chapter_task_contract(
            project_id,
            chapter_number,
            req,
            get_session=get_session,
            require_reason=require_reason,
            log_decision_event=log_decision_event,
        )

    def get_band_task_contract(project_id: str, band_id: str):
        return operations.get_band_task_contract(
            project_id,
            band_id,
            get_session=get_session,
        )

    def update_band_task_contract(
        project_id: str, band_id: str, req: TaskContractUpdateRequest
    ):
        return operations.update_band_task_contract(
            project_id,
            band_id,
            req,
            get_session=get_session,
            require_reason=require_reason,
            log_decision_event=log_decision_event,
        )

    def list_project_constraints(project_id: str):
        return operations.list_project_constraints(
            project_id,
            get_session=get_session,
            serialize_constraint=serialize_constraint,
        )

    def create_project_constraint(
        project_id: str, req: NarrativeConstraintCreateRequest
    ):
        return operations.create_project_constraint(
            project_id,
            req,
            get_session=get_session,
            require_reason=require_reason,
            validate_constraint_payload=validate_constraint_payload,
            log_decision_event=log_decision_event,
            serialize_constraint=serialize_constraint,
        )

    def update_project_constraint(
        project_id: str,
        constraint_id: str,
        req: NarrativeConstraintUpdateRequest,
    ):
        return operations.update_project_constraint(
            project_id,
            constraint_id,
            req,
            get_session=get_session,
            require_reason=require_reason,
            validate_constraint_payload=validate_constraint_payload,
            log_decision_event=log_decision_event,
            serialize_constraint=serialize_constraint,
            json_load_object=json_load_object,
        )

    def list_project_decision_events(
        project_id: str,
        scope: str = "",
        band_id: str = "",
        chapter_number: int = 0,
        task_id: str = "",
        event_family: str = "",
        related_object_type: str = "",
        related_object_id: str = "",
        causal_root_id: str = "",
    ):
        return operations.list_project_decision_events(
            project_id,
            get_session=get_session,
            list_decision_event_rows=list_decision_event_rows,
            serialize_decision_event=serialize_decision_event,
            scope=scope,
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
            event_family=event_family,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            causal_root_id=causal_root_id,
        )

    def get_project_causal_replay(
        project_id: str,
        scope: str = "cross_project",
        arc_id: str = "",
        band_id: str = "",
        chapter_number: int = 0,
        task_id: str = "",
    ):
        return operations.get_project_causal_replay(
            project_id,
            get_session=get_session,
            build_causal_replay=build_causal_replay,
            scope=scope,
            arc_id=arc_id,
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
        )

    def get_project_audit_insights(project_id: str):
        return operations.get_project_audit_insights(
            project_id,
            get_session=get_session,
            build_audit_insights=build_audit_insights,
        )

    def get_gate_ledger_report(
        scope: str = "project",
        project_id: str = "",
        band_id: str = "",
    ):
        return operations.get_gate_ledger_report(
            get_session=get_session,
            scope=scope,
            project_id=project_id,
            band_id=band_id,
        )

    def get_cost_report(
        project_id: str = "",
        chapter_number: int = 0,
        band_id: str = "",
        candidate_id: str = "",
    ):
        return operations.get_cost_report(
            get_session=get_session,
            project_id=project_id,
            chapter_number=chapter_number,
            band_id=band_id,
            candidate_id=candidate_id,
        )

    def get_latest_scenario_rehearsal(project_id: str):
        return operations.get_latest_scenario_rehearsal(
            project_id,
            get_session=get_session,
            display_datetime=display_datetime,
        )

    def rerun_scenario_rehearsal(project_id: str, run_id: str):
        return operations.rerun_scenario_rehearsal(
            project_id,
            run_id,
            get_session=get_session,
            display_datetime=display_datetime,
        )

    def approve_scenario_plan_patch(
        project_id: str,
        patch_id: str,
        req: ScenarioPlanPatchApproveRequest,
    ):
        return operations.approve_scenario_plan_patch(
            project_id,
            patch_id,
            reason=req.reason,
            get_session=get_session,
            display_datetime=display_datetime,
        )

    def get_trope_templates(category: str = "", q: str = "", limit: int = 0):
        return operations.get_trope_templates(
            category=category,
            q=q,
            limit=limit,
        )

    def get_trope_template_summary():
        return operations.get_trope_template_summary()

    def validate_trope_templates(req: TropeTemplateValidationRequest):
        return operations.validate_trope_templates(req)

    def override_band_experience(
        project_id: str,
        band_id: str,
        req: BandExperienceOverrideRequest,
    ):
        return operations.override_band_experience(
            project_id,
            band_id,
            req,
            get_session=get_session,
            pipeline=get_pipeline(),
        )

    return {
        "create_manual_checkpoint": create_manual_checkpoint,
        "get_band_checkpoint": get_band_checkpoint,
        "approve_band_checkpoint": approve_band_checkpoint,
        "get_chapter_task_contract": get_chapter_task_contract,
        "update_chapter_task_contract": update_chapter_task_contract,
        "get_band_task_contract": get_band_task_contract,
        "update_band_task_contract": update_band_task_contract,
        "list_project_constraints": list_project_constraints,
        "create_project_constraint": create_project_constraint,
        "update_project_constraint": update_project_constraint,
        "list_project_decision_events": list_project_decision_events,
        "get_project_causal_replay": get_project_causal_replay,
        "get_project_audit_insights": get_project_audit_insights,
        "get_gate_ledger_report": get_gate_ledger_report,
        "get_cost_report": get_cost_report,
        "get_latest_scenario_rehearsal": get_latest_scenario_rehearsal,
        "rerun_scenario_rehearsal": rerun_scenario_rehearsal,
        "approve_scenario_plan_patch": approve_scenario_plan_patch,
        "get_trope_templates": get_trope_templates,
        "get_trope_template_summary": get_trope_template_summary,
        "validate_trope_templates": validate_trope_templates,
        "override_band_experience": override_band_experience,
    }


class ProjectControlApplicationService:
    def __init__(self, deps: ProjectControlApplicationDeps) -> None:
        self._operations = _build_operations(deps)

    def handlers(self) -> dict[str, Callable[..., Any]]:
        return dict(self._operations)


__all__ = ["ProjectControlApplicationDeps", "ProjectControlApplicationService"]
