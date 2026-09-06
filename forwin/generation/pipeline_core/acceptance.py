from __future__ import annotations

from sqlalchemy.orm import Session

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.canon.review_recovery import reopen_failed_historical_candidate_for_review
from forwin.audit.events import DecisionActorType, DecisionEventType
from forwin.review.issue_groups import issue_group_for_issue
from forwin.maintenance.deferred import (
    DeferredMaintenanceRecord,
    record_deferred_maintenance,
)
from forwin.models.draft import ChapterDraft, ChapterReview


class AcceptanceStage:
    """Owns the acceptance stage behavior."""

    def accept_review(
        self,
        project_id: str,
        chapter_number: int,
        *,
        reason: str = "",
        actor_type: DecisionActorType = "manual_ui",
        actor_id: str = "",
        source: str = "direct",
    ) -> dict[str, str]:
        session: Session = self._SessionFactory()
        try:
            repo, updater, _checker = self._make_state_helpers(session)
            if repo.get_project(project_id) is None:
                raise ValueError(f"项目不存在: {project_id}")
            chapter_plan = repo.get_chapter_plan(project_id, chapter_number)
            if chapter_plan is None:
                raise ValueError(f"第{chapter_number}章不存在")
            chapter_status = str(chapter_plan.status or "")
            if chapter_status not in {"drafted", "needs_review"}:
                raise ValueError(
                    f"第{chapter_number}章不是可接受状态（当前 "
                    f"{chapter_status or 'unknown'}）"
                )

            latest_draft = (
                session.query(ChapterDraft)
                .filter(ChapterDraft.chapter_plan_id == chapter_plan.id)
                .order_by(ChapterDraft.version.desc(), ChapterDraft.id.desc())
                .first()
            )
            if latest_draft is None:
                raise ValueError(f"第{chapter_number}章尚未生成 draft")
            latest_review = (
                session.query(ChapterReview)
                .filter(ChapterReview.draft_id == latest_draft.id)
                .order_by(ChapterReview.created_at.desc(), ChapterReview.id.desc())
                .first()
            )
            if latest_review is None:
                raise ValueError(f"第{chapter_number}章尚未生成 review")
            candidate = CandidateDraftRepository(session).latest_for_chapter(
                project_id=project_id,
                chapter_number=chapter_number,
            )
            if candidate is None or candidate.candidate_draft_id != latest_draft.id:
                raise ValueError(f"第{chapter_number}章缺少 v5 candidate record")

            if candidate.status == "failed":
                reopen_failed_historical_candidate_for_review(
                    session,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    candidate_id=candidate.id,
                    draft_id=latest_draft.id,
                    review_id=latest_review.id,
                )

            writer_output = self._load_writer_output_from_meta(
                latest_draft.llm_raw_response
            )
            verdict = self._load_review_verdict(latest_review)
            repair_attempt_count = int(chapter_plan.repair_attempt_count or 0)
            residual_issues = self._review_issue_payloads(verdict)
            preparation = self.canon_preparation.prepare(
                context=self.canon_preparation_context,
                session=session,
                repo=repo,
                updater=updater,
                candidate_id=candidate.id,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
                acceptance_mode="human_approved",
                repair_attempt_count=repair_attempt_count,
                residual_review_issues=residual_issues,
                canon_risk_level=(
                    "low" if verdict.verdict in {"pass", "warn"} else "high"
                ),
            )
            if preparation.blocked or preparation.plan is None:
                updater.mark_chapter_status(
                    project_id,
                    chapter_number,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=residual_issues,
                    canon_risk_level="high",
                )
                session.commit()
                return {
                    "status": "needs_review",
                    "message": (
                        f"第{chapter_number}章不具备 Canon 资格，已转为 needs_review。"
                    ),
                    "frozen_artifact": preparation.blocked_path,
                }

            session.commit()
            canon_outcome = self.canon_admission.commit_plan(preparation.plan)
            session.expire_all()
            repo, updater, _checker = self._make_state_helpers(session)
            if canon_outcome.blocked:
                updater.mark_chapter_status(
                    project_id,
                    chapter_number,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=residual_issues,
                    canon_risk_level="high",
                )
                session.commit()
                return {
                    "status": "needs_review",
                    "message": (
                        f"第{chapter_number}章 Canon 提交失败，已转为 needs_review。"
                    ),
                    "frozen_artifact": canon_outcome.blocked_path,
                }

            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="audit_action",
                event_type=DecisionEventType.REVIEW_APPROVED,
                actor_type=actor_type,
                actor_id=str(actor_id or ""),
                scope="chapter",
                summary=f"第{chapter_number}章 review 已人工接受并写入 Canon。",
                reason=str(reason or "").strip(),
                related_object_type="chapter_review",
                related_object_id=latest_review.id,
                payload={
                    "issue_types": [
                        str(
                            getattr(
                                issue,
                                "issue_type",
                                getattr(issue, "rule_name", ""),
                            )
                            or ""
                        )
                        for issue in verdict.issues
                    ],
                    "issue_groups": [
                        str(
                            getattr(issue, "issue_group", "")
                            or issue_group_for_issue(
                                issue_type=str(getattr(issue, "issue_type", "") or ""),
                                rule_name=str(getattr(issue, "rule_name", "") or ""),
                            )
                        )
                        for issue in verdict.issues
                    ],
                    "verdict": verdict.verdict,
                    "canon_commit_id": canon_outcome.commit_id,
                    "source": str(source or ""),
                },
            )
            session.commit()

            maintenance_deferred = False
            maintenance_blockers: list[str] = []
            maintenance_run_ids: list[str] = []
            try:
                phase3 = self._run_phase3_pass(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    canon_commit_id=canon_outcome.commit_id,
                )
                maintenance_run_ids = list(
                    dict(phase3.get("run_ids") or {}).values()
                )
                self._run_post_canon_order_controls(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    trigger_stage="manual_acceptance",
                    canon_commit_id=canon_outcome.commit_id,
                )
                maintenance_blockers = (
                    self.post_canon_maintenance.barrier_blocking_reasons(
                        canon_outcome.commit_id
                    )
                )
                maintenance_deferred = bool(maintenance_blockers)
            except Exception as exc:  # noqa: BLE001
                maintenance_deferred = True
                session.rollback()
                maintenance_run_ids = list(
                    getattr(exc, "maintenance_run_ids", maintenance_run_ids)
                    or maintenance_run_ids
                )
                _repo, updater, _checker = self._make_state_helpers(session)
                record_deferred_maintenance(
                    updater,
                    DeferredMaintenanceRecord(
                        project_id=project_id,
                        chapter_number=chapter_number,
                        task_type="post_canon_phase3",
                        reason=str(exc),
                        payload={
                            "error_class": exc.__class__.__name__,
                            "canon_commit_id": canon_outcome.commit_id,
                        },
                        maintenance_run_ids=maintenance_run_ids,
                    ),
                )
                session.commit()

            if maintenance_blockers:
                _repo, updater, _checker = self._make_state_helpers(session)
                record_deferred_maintenance(
                    updater,
                    DeferredMaintenanceRecord(
                        project_id=project_id,
                        chapter_number=chapter_number,
                        task_type="post_canon_order_controls_blocked",
                        reason="; ".join(maintenance_blockers),
                        payload={"canon_commit_id": canon_outcome.commit_id},
                        maintenance_run_ids=maintenance_run_ids,
                    ),
                )
                session.commit()

            return {
                "status": (
                    "maintenance_pending" if maintenance_deferred else "accepted"
                ),
                "message": (
                    f"第{chapter_number}章已接受并写入 Canon；后置维护或顺序控制待恢复。"
                    if maintenance_deferred
                    else f"第{chapter_number}章已接受并写入 Canon。"
                ),
                "frozen_artifact": "",
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


__all__ = ["AcceptanceStage"]
