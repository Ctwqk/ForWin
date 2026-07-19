from __future__ import annotations

from fastapi import HTTPException

from forwin.api_schema.policy import RuntimePolicyResponse, RuntimePolicyUpdateRequest
from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
)
from forwin.models.project import Project
from forwin.runtime.policy_store import (
    ProjectPolicyMissing,
    ProjectPolicyStore,
    ProjectPolicyVersionConflict,
)
from forwin.state.updater import StateUpdater


def get_project_policy(
    project_id: str,
    *,
    session_factory,
) -> RuntimePolicyResponse:
    session = session_factory()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        try:
            record = ProjectPolicyStore(session).load(project)
        except ProjectPolicyMissing as exc:
            raise HTTPException(500, "project runtime policy is missing") from exc
        return RuntimePolicyResponse(
            project_id=project_id,
            version=record.version,
            policy=record.policy,
            message="已读取项目运行策略。",
        )
    finally:
        session.close()


def update_project_policy(
    project_id: str,
    request: RuntimePolicyUpdateRequest,
    *,
    session_factory,
) -> RuntimePolicyResponse:
    reason = request.reason.strip()
    if not reason:
        raise HTTPException(400, "更新运行策略必须填写原因")
    session = session_factory()
    try:
        with session.begin():
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "项目不存在")
            store = ProjectPolicyStore(session)
            try:
                current = store.load(project)
            except ProjectPolicyMissing as exc:
                raise HTTPException(500, "project runtime policy is missing") from exc
            next_policy = current.policy.with_user_settings(
                quality_profile=request.quality_profile,
                model_profile_id=request.model_profile_id,
                min_chars=request.min_chapter_chars,
                target_chars=request.target_chapter_chars,
                max_chars=request.max_chapter_chars,
                review_interval_chapters=request.review_interval_chapters,
                manual_checkpoints=request.manual_checkpoints,
                band_checkpoint_action=request.band_checkpoint_action,
                gate_delegate=request.gate_delegate,
            )
            try:
                saved = store.save(
                    project,
                    next_policy,
                    expected_version=request.expected_version,
                )
            except ProjectPolicyVersionConflict as exc:
                raise HTTPException(409, "runtime policy version conflict") from exc
            StateUpdater(session).save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    event_family="audit_action",
                    event_type=DecisionEventType.RUNTIME_POLICY_UPDATED,
                    actor_type="api",
                    summary="项目运行策略已更新。",
                    reason=reason,
                    payload={
                        "previous_version": current.version,
                        "new_version": saved.version,
                    },
                )
            )
        return RuntimePolicyResponse(
            project_id=project_id,
            version=saved.version,
            policy=saved.policy,
            message="项目运行策略已保存。",
        )
    finally:
        session.close()
