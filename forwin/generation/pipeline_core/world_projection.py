from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import attach_gate_outcome
from forwin.generation.pipeline_core import chapter_execution_support
from forwin.generation.pipeline_core.obligation_resolution import (
    _verify_obligations_after_acceptance,
)
from forwin.maintenance.post_canon import (
    PostCanonMaintenanceBlocked,
)
from forwin.maintenance.state import post_canon_control_blockers
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.planning_control import BandCheckpoint
from forwin.planning.checkpoints import BandCheckpointDetail, BandCheckpointIssueInfo
from forwin.protocol.writer import WriterOutput
from forwin.review.issue_groups import issue_group_for_issue


def _prompt_trace_success_summary(
    writer_output: WriterOutput,
) -> dict[str, object]:
    generation_meta = getattr(writer_output, "generation_meta", {}) or {}
    prompt_trace = (
        generation_meta.get("prompt_trace") if isinstance(generation_meta, dict) else {}
    )
    attempts = (
        prompt_trace.get("attempts", []) if isinstance(prompt_trace, dict) else []
    )
    if not isinstance(attempts, list):
        attempts = []
    successful = None
    for item in attempts:
        if not isinstance(item, dict):
            continue
        if int(item.get("output_chars") or 0) > 0 and not str(
            item.get("error_class") or ""
        ):
            successful = item
    if successful is None and attempts:
        successful = next(
            (item for item in reversed(attempts) if isinstance(item, dict)),
            None,
        )
    if not isinstance(successful, dict):
        return {
            "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
            "effective_model": "",
            "effective_profile_id": "",
            "successful_attempt_no": 0,
            "attempt_group_id": "",
            "output_chars": int(getattr(writer_output, "char_count", 0) or 0),
            "fallback_chain": generation_meta.get("model_fallbacks", []),
        }
    return {
        "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
        "effective_model": str(successful.get("model") or ""),
        "effective_profile_id": str(successful.get("profile_id") or ""),
        "effective_profile_name": str(successful.get("profile_name") or ""),
        "successful_attempt_no": int(successful.get("attempt_no") or 0),
        "attempt_group_id": str(successful.get("attempt_group_id") or ""),
        "output_chars": int(
            successful.get("output_chars")
            or getattr(writer_output, "char_count", 0)
            or 0
        ),
        "fallback_chain": generation_meta.get("model_fallbacks", []),
    }


class PostCanonStage:
    """Owns durable post-Canon maintenance and ordering controls."""

    def _run_phase3_pass(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        canon_commit_id: str = "",
    ) -> dict[str, Any]:
        session.commit()
        result = self.post_canon_maintenance.run_for_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
            canon_commit_id=canon_commit_id,
            worker_id=self._post_canon_worker_id(
                chapter_number=chapter_number,
                purpose="phase3",
            ),
        )
        session.expire_all()
        return result

    def _run_post_canon_order_controls(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        trigger_stage: str,
        canon_commit_id: str = "",
    ) -> dict[str, Any]:
        session.commit()
        commit_id = str(canon_commit_id or "").strip()
        if not commit_id:
            commit_id = self.post_canon_maintenance.resolve_canon_commit(
                project_id=project_id,
                chapter_number=chapter_number,
            )

        def run_controls(control_session: Session, commit) -> dict[str, Any]:  # noqa: ANN001
            repo, updater, _checker = self._make_state_helpers(control_session)
            candidate = control_session.get(CandidateDraftRecord, commit.candidate_id)
            if candidate is None:
                raise ValueError("post-Canon candidate is unavailable")
            draft = control_session.get(ChapterDraft, candidate.candidate_draft_id)
            if draft is None:
                raise ValueError("accepted post-Canon draft is unavailable")
            _verify_obligations_after_acceptance(
                self,
                session=control_session,
                project_id=commit.project_id,
                chapter_number=commit.chapter_number,
                accepted_text=str(draft.body_text or ""),
            )
            future_audit = self._audit_future_plans_after_acceptance(
                session=control_session,
                updater=updater,
                project_id=commit.project_id,
                chapter_number=commit.chapter_number,
                trigger_stage=trigger_stage,
            )
            self._record_generation_audit_report_if_due(
                session=control_session,
                updater=updater,
                project_id=commit.project_id,
                chapter_number=commit.chapter_number,
                future_plan_audit_result=future_audit,
            )
            checkpoint = self._run_post_canon_band_checkpoint(
                session=control_session,
                repo=repo,
                updater=updater,
                project_id=commit.project_id,
                chapter_number=commit.chapter_number,
            )
            return {
                "project_id": commit.project_id,
                "chapter_number": int(commit.chapter_number or 0),
                "trigger_stage": trigger_stage,
                "blocking_reasons": (
                    list(future_audit.blocking_reasons)
                    if future_audit is not None
                    else []
                ),
                "future_plan_audit": (
                    future_audit.model_dump(mode="json")
                    if future_audit is not None
                    else None
                ),
                "checkpoint": checkpoint,
            }

        result = self.post_canon_maintenance.run_order_controls(
            canon_commit_id=commit_id,
            runner=run_controls,
        )
        session.expire_all()
        return result

    def _run_post_canon_band_checkpoint(
        self,
        *,
        session: Session,
        repo,
        updater,
        project_id: str,
        chapter_number: int,
    ) -> dict[str, Any] | None:
        project = repo.get_project(project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        policy = self._project_policy(session, project)
        if policy.pause.band_checkpoint_action == "continue":
            return None
        try:
            checkpoint = self._create_auto_band_checkpoint(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
            )
        except Exception as exc:
            band_row = repo.get_band_row_for_chapter(project_id, chapter_number)
            if band_row is None:
                raise
            checkpoint = updater.save_band_checkpoint(
                BandCheckpointDetail(
                    project_id=project_id,
                    arc_id=band_row.arc_id,
                    band_id=band_row.band_id,
                    chapter_start=int(band_row.chapter_start or 0),
                    chapter_end=int(band_row.chapter_end or 0),
                    trigger_source="auto_band_end",
                    boundary_kind="band_end",
                    boundary_chapter=chapter_number,
                    status="error",
                    summary="band checkpoint evaluator exception; run paused.",
                    issues=[
                        BandCheckpointIssueInfo(
                            code="checkpoint_evaluator_error",
                            severity="error",
                            issue_group=issue_group_for_issue(code="runtime"),
                            description="band checkpoint evaluator failed.",
                            detail=f"{exc.__class__.__name__}: {exc}",
                        )
                    ],
                )
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                band_id=band_row.band_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
                scope="band",
                summary="band checkpoint evaluator failed.",
                reason=str(exc),
                related_object_type="band_checkpoint",
                related_object_id=checkpoint.id,
                payload=attach_gate_outcome(
                    {
                        "status": "error",
                        "error_class": exc.__class__.__name__,
                        "error_summary": str(exc),
                    },
                    chapter_execution_support.checkpoint_event_gate_outcome(
                        checkpoint,
                        chapter_number=chapter_number,
                        policy_version=int(
                            getattr(project, "runtime_policy_version", 0) or 0
                        ),
                        decision="error",
                        blocked=True,
                    ),
                ),
            )
        if checkpoint is None:
            return None
        return {
            "id": checkpoint.id,
            "band_id": checkpoint.band_id,
            "status": checkpoint.status,
        }

    def _recover_post_canon_before_chapter(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> None:
        session.commit()
        recovered: set[str] = set()

        def recover(commit_id: str) -> None:
            if commit_id in recovered:
                raise PostCanonMaintenanceBlocked(
                    f"cyclic post-Canon dependency detected at {commit_id}",
                    canon_commit_id=commit_id,
                )
            recovered.add(commit_id)
            try:
                self.post_canon_maintenance.run(
                    canon_commit_id=commit_id,
                    worker_id=self._post_canon_worker_id(
                        chapter_number=chapter_number,
                        purpose="preflight",
                    ),
                )
            except PostCanonMaintenanceBlocked as exc:
                dependency_id = str(exc.canon_commit_id or "").strip()
                if not dependency_id or dependency_id == commit_id:
                    raise
                recover(dependency_id)
                self.post_canon_maintenance.run(
                    canon_commit_id=commit_id,
                    worker_id=self._post_canon_worker_id(
                        chapter_number=chapter_number,
                        purpose="preflight",
                    ),
                )
            controls = self._run_post_canon_order_controls(
                session=session,
                project_id=project_id,
                chapter_number=chapter_number - 1,
                trigger_stage="continuation_preflight",
                canon_commit_id=commit_id,
            )
            self._resume_post_canon_checkpoint_delegation(
                session=session,
                project_id=project_id,
                chapter_number=int(
                    controls.get("chapter_number") or chapter_number - 1
                ),
                controls=controls,
            )
            self.post_canon_maintenance.assert_barrier_ready(commit_id)

        for commit_id in self.post_canon_maintenance.commits_before_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
        ):
            recover(commit_id)
        session.expire_all()

    def _resume_post_canon_checkpoint_delegation(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        controls: dict[str, Any],
    ) -> None:
        checkpoint_payload = controls.get("checkpoint")
        if not isinstance(checkpoint_payload, dict):
            return
        checkpoint_id = str(checkpoint_payload.get("id") or "").strip()
        if not checkpoint_id:
            return
        checkpoint = session.get(BandCheckpoint, checkpoint_id)
        if checkpoint is None:
            return
        repo, updater, _checker = self._make_state_helpers(session)
        project = repo.get_project(project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        policy = self._project_policy(session, project)
        status = str(checkpoint.status or "")
        should_resolve = status in {"fail", "error"} or (
            status == "warn"
            and policy.pause.band_checkpoint_action
            in {"pause_on_warn", "pause_always"}
        )
        if not should_resolve:
            return
        self._resolve_checkpoint_gate(
            updater=updater,
            checkpoint=checkpoint,
            gate_kind="band_checkpoint_pause",
            chapter_number=chapter_number,
        )
        session.commit()

    @staticmethod
    def _post_canon_control_blocking_reasons(
        result: dict[str, Any],
    ) -> list[str]:
        return post_canon_control_blockers(result)

    def _post_canon_worker_id(self, *, chapter_number: int, purpose: str) -> str:
        task_id = str(getattr(self, "_audit_task_id", "") or "direct")
        return f"pipeline:{task_id}:{int(chapter_number or 0)}:{purpose}"

    @staticmethod
    def _prompt_trace_success_summary(writer_output: WriterOutput) -> dict[str, object]:
        return _prompt_trace_success_summary(writer_output)


__all__ = ["PostCanonStage"]
