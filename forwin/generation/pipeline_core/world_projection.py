from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from forwin.maintenance.post_canon import (
    PostCanonMaintenanceBlocked,
)
from forwin.maintenance.state import post_canon_control_blockers


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

        from forwin.maintenance.order_controls import PostCanonOrderControls

        owner = PostCanonOrderControls(
            policy=self.policy, llm_client=self.llm_client,
            trace_recorder=self.trace_recorder,
            record_event=self._record_decision_event,
            checkpoint_evaluator=self._create_auto_band_checkpoint,
            resolve_checkpoint=self._resolve_checkpoint_gate,
        )

        def run_controls(control_session, commit):
            return owner.run(control_session, commit, trigger_stage=trigger_stage)

        result = self.post_canon_maintenance.run_order_controls(
            canon_commit_id=commit_id,
            runner=run_controls,
            finalizer=owner.finalize,
            requires_delegation=owner.requires_delegation,
        )
        session.expire_all()
        return result

    def _run_post_canon_band_checkpoint(self, **kwargs):
        from forwin.maintenance.order_controls import PostCanonOrderControls

        return PostCanonOrderControls(
            policy=self.policy, trace_recorder=self.trace_recorder,
            record_event=self._record_decision_event,
            checkpoint_evaluator=self._create_auto_band_checkpoint,
        )._run_post_canon_band_checkpoint(**kwargs)

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
        from forwin.maintenance.order_controls import resume_checkpoint_delegation

        resume_checkpoint_delegation(
            session=session, project_id=project_id, chapter_number=chapter_number,
            controls=controls, policy=self.policy,
            resolve_checkpoint=self._resolve_checkpoint_gate,
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


__all__ = ["PostCanonStage"]
