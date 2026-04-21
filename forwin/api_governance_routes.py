from __future__ import annotations

from typing import Any, Callable

from forwin import api_governance_ops
from forwin.api_schemas import (
    BandCheckpointApproveRequest,
    BandExperienceOverrideRequest,
    ManualCheckpointRequest,
    NarrativeConstraintCreateRequest,
    NarrativeConstraintUpdateRequest,
    ProjectGovernanceUpdateRequest,
    TaskContractUpdateRequest,
    TropeTemplateValidationRequest,
)


def build_handlers(
    *,
    get_session: Callable[[], Any],
    get_config: Callable[[], Any],
    get_orchestrator: Callable[[], Any],
    display_datetime: Callable[[Any], str],
    require_reason: Callable[[str], str],
    validate_constraint_payload: Callable[..., tuple[str, str, str]],
    serialize_band_checkpoint: Callable[..., Any],
    serialize_constraint: Callable[..., Any],
    list_decision_event_rows: Callable[..., list[Any]],
    serialize_decision_event: Callable[[Any], Any],
    build_causal_replay: Callable[..., Any],
    build_governance_insights: Callable[..., Any],
    latest_band_checkpoint_row: Callable[..., Any],
    latest_related_decision_event: Callable[..., Any],
    resolve_project_governance: Callable[..., Any],
    governance_request_payload: Callable[[object], dict[str, object]],
    persist_project_governance: Callable[..., Any],
    log_decision_event: Callable[..., Any],
    json_load_object: Callable[[str | None], dict[str, Any]],
) -> dict[str, Callable[..., Any]]:
    def get_project_governance(project_id: str):
        return api_governance_ops.get_project_governance(
            project_id,
            get_session=get_session,
            config=get_config(),
            resolve_project_governance=resolve_project_governance,
        )

    def update_project_governance(project_id: str, req: ProjectGovernanceUpdateRequest):
        return api_governance_ops.update_project_governance(
            project_id,
            req,
            get_session=get_session,
            config=get_config(),
            require_reason=require_reason,
            governance_request_payload=governance_request_payload,
            resolve_project_governance=resolve_project_governance,
            persist_project_governance=persist_project_governance,
            log_decision_event=log_decision_event,
        )

    def create_manual_checkpoint(project_id: str, req: ManualCheckpointRequest):
        return api_governance_ops.create_manual_checkpoint(
            project_id,
            req,
            get_session=get_session,
            config=get_config(),
            require_reason=require_reason,
            resolve_project_governance=resolve_project_governance,
            serialize_band_checkpoint=serialize_band_checkpoint,
            log_decision_event=log_decision_event,
        )

    def get_band_checkpoint(project_id: str, band_id: str):
        return api_governance_ops.get_band_checkpoint(
            project_id,
            band_id,
            get_session=get_session,
            latest_band_checkpoint_row=latest_band_checkpoint_row,
            serialize_band_checkpoint=serialize_band_checkpoint,
        )

    def approve_band_checkpoint(project_id: str, band_id: str, req: BandCheckpointApproveRequest):
        return api_governance_ops.approve_band_checkpoint(
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
        return api_governance_ops.get_chapter_task_contract(
            project_id,
            chapter_number,
            get_session=get_session,
        )

    def update_chapter_task_contract(
        project_id: str,
        chapter_number: int,
        req: TaskContractUpdateRequest,
    ):
        return api_governance_ops.update_chapter_task_contract(
            project_id,
            chapter_number,
            req,
            get_session=get_session,
            require_reason=require_reason,
            log_decision_event=log_decision_event,
        )

    def get_band_task_contract(project_id: str, band_id: str):
        return api_governance_ops.get_band_task_contract(
            project_id,
            band_id,
            get_session=get_session,
        )

    def update_band_task_contract(project_id: str, band_id: str, req: TaskContractUpdateRequest):
        return api_governance_ops.update_band_task_contract(
            project_id,
            band_id,
            req,
            get_session=get_session,
            require_reason=require_reason,
            log_decision_event=log_decision_event,
        )

    def list_project_constraints(project_id: str):
        return api_governance_ops.list_project_constraints(
            project_id,
            get_session=get_session,
            serialize_constraint=serialize_constraint,
        )

    def create_project_constraint(project_id: str, req: NarrativeConstraintCreateRequest):
        return api_governance_ops.create_project_constraint(
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
        return api_governance_ops.update_project_constraint(
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
        return api_governance_ops.list_project_decision_events(
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
        scope: str = "project",
        arc_id: str = "",
        band_id: str = "",
        chapter_number: int = 0,
        task_id: str = "",
    ):
        return api_governance_ops.get_project_causal_replay(
            project_id,
            get_session=get_session,
            build_causal_replay=build_causal_replay,
            scope=scope,
            arc_id=arc_id,
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
        )

    def get_project_governance_insights(project_id: str):
        return api_governance_ops.get_project_governance_insights(
            project_id,
            get_session=get_session,
            build_governance_insights=build_governance_insights,
        )

    def get_latest_provisional_band(project_id: str):
        return api_governance_ops.get_latest_provisional_band(
            project_id,
            get_session=get_session,
            display_datetime=display_datetime,
        )

    def get_trope_templates(category: str = "", q: str = "", limit: int = 0):
        return api_governance_ops.get_trope_templates(
            category=category,
            q=q,
            limit=limit,
        )

    def get_trope_template_summary():
        return api_governance_ops.get_trope_template_summary()

    def validate_trope_templates(req: TropeTemplateValidationRequest):
        return api_governance_ops.validate_trope_templates(req)

    def override_band_experience(
        project_id: str,
        band_id: str,
        req: BandExperienceOverrideRequest,
    ):
        return api_governance_ops.override_band_experience(
            project_id,
            band_id,
            req,
            get_session=get_session,
            orchestrator=get_orchestrator(),
        )

    return {
        "get_project_governance": get_project_governance,
        "update_project_governance": update_project_governance,
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
        "get_project_governance_insights": get_project_governance_insights,
        "get_latest_provisional_band": get_latest_provisional_band,
        "get_trope_templates": get_trope_templates,
        "get_trope_template_summary": get_trope_template_summary,
        "validate_trope_templates": validate_trope_templates,
        "override_band_experience": override_band_experience,
    }
