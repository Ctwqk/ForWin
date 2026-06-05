from __future__ import annotations

from forwin.canon_quality.obligation_verifier import (
    expire_unresolved_obligations_after_acceptance,
    verify_active_obligations_after_acceptance,
)
from forwin.orchestrator_loop_core.common import *

def accept_review(self, project_id: str, chapter_number: int, *, reason: str = "") -> dict[str, str]:
    session: Session = self._SessionFactory()
    try:
        repo, updater, _checker = self._make_state_helpers(session)
        project = repo.get_project(project_id)
        chapter_plan = repo.get_chapter_plan(project_id, chapter_number)
        if chapter_plan is None:
            raise ValueError(f"第{chapter_number}章不存在")

        latest_draft = session.query(ChapterDraft).filter(
            ChapterDraft.chapter_plan_id == chapter_plan.id
        ).order_by(ChapterDraft.version.desc()).first()
        if latest_draft is None:
            raise ValueError(f"第{chapter_number}章尚未生成 draft")

        latest_review = session.query(ChapterReview).filter(
            ChapterReview.draft_id == latest_draft.id
        ).order_by(ChapterReview.created_at.desc()).first()
        if latest_review is None:
            raise ValueError(f"第{chapter_number}章尚未生成 review")

        writer_output = self._load_writer_output_from_meta(latest_draft.llm_raw_response)
        verdict = self._load_review_verdict(latest_review)

        from forwin.orchestrator_loop_core.project_chapters import (
            _coerce_canon_apply_outcome,
        )

        canon_outcome = _coerce_canon_apply_outcome(
            self._apply_canon_candidate(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
            )
        )

        repair_attempt_count = len(repo.list_chapter_rewrite_attempts(project_id, chapter_number))
        frozen_path = canon_outcome.blocked_path
        if canon_outcome.blocked:
            updater.mark_chapter_status(
                project_id,
                chapter_number,
                "needs_review",
                repair_attempt_count=repair_attempt_count,
                residual_review_issues=self._review_issue_payloads(verdict),
                canon_risk_level="high",
            )
            session.commit()
            return {
                "status": "needs_review",
                "message": f"第{chapter_number}章 canon gate 阻止接受，已转为 needs_review。",
                "frozen_artifact": frozen_path,
            }
        acceptance_mode = (
            "checkpoint_approved"
            if project is not None and self._project_governance(project).default_operation_mode == "checkpoint"
            else "human_approved"
        )
        updater.mark_chapter_status(
            project_id,
            chapter_number,
            "accepted",
            acceptance_mode=acceptance_mode,
            repair_attempt_count=repair_attempt_count,
            residual_review_issues=self._review_issue_payloads(verdict),
            canon_risk_level=(
                "low" if verdict.verdict in {"pass", "warn"} else "high"
            ),
        )
        self.retrieval_broker.memory_index.upsert_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
            title=writer_output.title,
            summary=writer_output.end_of_chapter_summary,
            body=writer_output.body,
        )
        self._run_phase3_pass(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        )
        self._audit_future_plans_after_acceptance(
            session=session,
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            trigger_stage="manual_acceptance",
        )
        obligation_verifier_payload: dict[str, object] = {}
        if bool(getattr(self.config, "review_engine_obligation_verifier_enabled", False)):
            obligation_verifier_payload = {
                "resolution": verify_active_obligations_after_acceptance(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    accepted_text=writer_output.body,
                ),
                "expiry": expire_unresolved_obligations_after_acceptance(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_number,
                ),
            }
        self._compile_world_model_after_acceptance(
            session=session,
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.REVIEW_APPROVED,
            scope="chapter",
            summary=f"第{chapter_number}章 review 已人工接受并写入 canon。",
            reason=str(reason or "").strip(),
            related_object_type="chapter_review",
            related_object_id=latest_review.id,
            payload={
                "issue_types": [
                    str(getattr(issue, "issue_type", getattr(issue, "rule_name", "")) or "")
                    for issue in verdict.issues
                ],
                "issue_groups": [
                    str(getattr(issue, "issue_group", "") or issue_group_for_issue(
                        issue_type=str(getattr(issue, "issue_type", "") or ""),
                        rule_name=str(getattr(issue, "rule_name", "") or ""),
                    ))
                    for issue in verdict.issues
                ],
                "verdict": verdict.verdict,
                "obligation_verifier": obligation_verifier_payload,
            },
        )
        session.commit()
        return {
            "status": "accepted",
            "message": f"第{chapter_number}章已接受并写入 canon。",
            "frozen_artifact": frozen_path,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()



__all__ = ['accept_review']
