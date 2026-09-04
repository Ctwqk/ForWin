from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import threading
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from forwin.audience.feedback import run_feedback_aggregation_pass
from forwin.models.base import new_id
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.project import Project
from forwin.maintenance.events import (
    ORDER_CONTROLS_KEY,
    POST_CANON_STEP_NAMES,
)
from forwin.maintenance.state import (
    post_canon_barrier_blockers,
    post_canon_barrier_ready,
    post_canon_order_controls,
    post_canon_phase3_complete,
)
from forwin.maintenance.trace_upload import enqueue_trace_upload
from forwin.observability.payloads import safe_error_summary
from forwin.planning.stage_analysis import save_stage_analysis
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.simulation.world import save_world_turn


MAX_ERROR_LENGTH = 4000


class PostCanonMaintenanceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        canon_commit_id: str = "",
        run_id: str = "",
        step_name: str = "",
    ) -> None:
        super().__init__(message)
        self.canon_commit_id = canon_commit_id
        self.run_id = run_id
        self.step_name = step_name

    @property
    def maintenance_run_ids(self) -> list[str]:
        return [self.run_id] if self.run_id else []


class PostCanonMaintenanceBusy(PostCanonMaintenanceError):
    """A live worker owns the requested maintenance step."""


class PostCanonMaintenanceBlocked(PostCanonMaintenanceError):
    """An earlier step or Canon chapter has not completed its barrier."""


class PostCanonLeaseLost(PostCanonMaintenanceError):
    """A stale worker attempted to heartbeat or complete a reclaimed run."""


@dataclass(frozen=True, slots=True)
class PostCanonRunClaim:
    run_id: str
    canon_commit_id: str
    project_id: str
    chapter_number: int
    candidate_id: str
    step_name: str
    worker_id: str
    lease_epoch: int
    lease_seconds: float


StepRunner = Callable[[Session, CanonCommitRecord], Mapping[str, Any]]
OrderControlsRunner = Callable[[Session, CanonCommitRecord], Mapping[str, Any]]


class _ClaimHeartbeat:
    def __init__(
        self,
        *,
        service: PostCanonMaintenanceService,
        claim: PostCanonRunClaim,
        interval_seconds: float,
    ) -> None:
        self._service = service
        self._claim = claim
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._ownership_lost = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"post-canon-heartbeat-{claim.run_id}",
            daemon=True,
        )

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost.is_set()

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def stop_and_join(self) -> None:
        self._stop.set()
        if not self._started:
            return
        self._thread.join(timeout=max(0.2, self._interval_seconds * 2))
        if self._thread.is_alive():
            self._ownership_lost.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._service.heartbeat(self._claim)
            except Exception:  # noqa: BLE001
                self._ownership_lost.set()
                return


class PostCanonMaintenanceService:
    """Runs the four ordered post-Canon steps behind durable fenced leases."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        stage_analyzer: Any,
        pacing_strategist: Any,
        replan_governor: Any,
        arc_envelope_manager: Any,
        world_simulator: Any,
        artifact_store: Any,
        llm_client: Any,
        lease_seconds: float = 300.0,
        heartbeat_interval_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.stage_analyzer = stage_analyzer
        self.pacing_strategist = pacing_strategist
        self.replan_governor = replan_governor
        self.arc_envelope_manager = arc_envelope_manager
        self.world_simulator = world_simulator
        self.artifact_store = artifact_store
        self.llm_client = llm_client
        self.lease_seconds = _positive_seconds(lease_seconds, "lease_seconds")
        default_heartbeat = min(30.0, self.lease_seconds / 3.0)
        resolved_heartbeat = (
            default_heartbeat
            if heartbeat_interval_seconds is None
            else heartbeat_interval_seconds
        )
        self.heartbeat_interval_seconds = _positive_seconds(
            resolved_heartbeat,
            "heartbeat_interval_seconds",
        )
        if self.heartbeat_interval_seconds >= self.lease_seconds:
            raise ValueError(
                "heartbeat_interval_seconds must be less than lease_seconds"
            )
        self._clock = clock or utcnow
        self._step_runners: dict[str, StepRunner] = {
            "planning": self._run_planning_step,
            "arc": self._run_arc_step,
            "world": self._run_world_step,
            "feedback": self._run_feedback_step,
        }

    def resolve_canon_commit(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> str:
        normalized_project = str(project_id or "").strip()
        normalized_chapter = int(chapter_number or 0)
        with self.session_factory() as session:
            commit = session.execute(
                select(CanonCommitRecord).where(
                    CanonCommitRecord.project_id == normalized_project,
                    CanonCommitRecord.chapter_number == normalized_chapter,
                    CanonCommitRecord.status == "committed",
                )
            ).scalar_one_or_none()
        if commit is None:
            raise ValueError(
                f"Committed Canon not found for {normalized_project} chapter "
                f"{normalized_chapter}"
            )
        return commit.id

    def resolve_event_canon_commit(
        self,
        *,
        canon_commit_id: str,
        canon_idempotency_key: str,
        project_id: str,
        chapter_number: int,
        candidate_id: str,
    ) -> str:
        commit_id = str(canon_commit_id or "").strip()
        normalized_canon_key = str(canon_idempotency_key or "").strip()
        normalized_project = str(project_id or "").strip()
        normalized_chapter = int(chapter_number or 0)
        normalized_candidate = str(candidate_id or "").strip()
        if not commit_id:
            raise ValueError("post-Canon event requires canon_commit_id")
        if not normalized_canon_key:
            raise ValueError("post-Canon event requires canon_idempotency_key")
        if not normalized_project:
            raise ValueError("post-Canon event requires project_id")
        if normalized_chapter < 1:
            raise ValueError("post-Canon event requires a positive chapter_number")
        if not normalized_candidate:
            raise ValueError("post-Canon event requires candidate_id")
        with self.session_factory() as session:
            commit = session.get(CanonCommitRecord, commit_id)
            if (
                commit is None
                or commit.status != "committed"
                or commit.idempotency_key != normalized_canon_key
                or commit.project_id != normalized_project
                or int(commit.chapter_number or 0) != normalized_chapter
                or commit.candidate_id != normalized_candidate
            ):
                raise ValueError(
                    "post-Canon event identity does not match committed Canon"
                )
            return commit.id

    def commits_before_chapter(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> list[str]:
        with self.session_factory() as session:
            commit_id = session.execute(
                select(CanonCommitRecord.id)
                .where(
                    CanonCommitRecord.project_id == str(project_id or "").strip(),
                    CanonCommitRecord.chapter_number < int(chapter_number or 0),
                    CanonCommitRecord.status == "committed",
                )
                .order_by(CanonCommitRecord.chapter_number.desc())
                .limit(1)
            ).scalar_one_or_none()
            return [str(commit_id)] if commit_id else []

    def run_ids_for_commit(self, canon_commit_id: str) -> dict[str, str]:
        with self.session_factory() as session:
            return {
                row.step_name: row.id
                for row in session.execute(
                    select(PostCanonMaintenanceRun).where(
                        PostCanonMaintenanceRun.canon_commit_id
                        == str(canon_commit_id or "").strip()
                    )
                ).scalars()
            }

    def barrier_blocking_reasons(self, canon_commit_id: str) -> list[str]:
        commit_id = str(canon_commit_id or "").strip()
        with self.session_factory() as session:
            commit = session.get(CanonCommitRecord, commit_id)
            if commit is None or commit.status != "committed":
                raise ValueError(f"Committed Canon not found: {commit_id}")
            rows = list(
                session.execute(
                    select(PostCanonMaintenanceRun).where(
                        PostCanonMaintenanceRun.canon_commit_id == commit_id
                    )
                ).scalars()
            )
            if not post_canon_phase3_complete(rows):
                return ["post_canon_phase3_incomplete"]
            controls = post_canon_order_controls(rows)
            project = session.get(Project, commit.project_id)
            if project is None:
                raise ValueError(f"Project not found: {commit.project_id}")
            action = (
                ProjectPolicyStore(session)
                .load(project)
                .policy.pause.band_checkpoint_action
            )
            blockers = post_canon_barrier_blockers(
                rows,
                session=session,
                band_checkpoint_action=action,
            )
            status = str(controls.get("status") or "pending")
            if status in {"succeeded", "blocked"}:
                return blockers or (
                    []
                    if status == "succeeded"
                    else ["post_canon_order_controls_blocked"]
                )
            return [f"post_canon_order_controls_{status}"]

    def assert_barrier_ready(self, canon_commit_id: str) -> None:
        blockers = self.barrier_blocking_reasons(canon_commit_id)
        if not blockers:
            return
        run_id = self.run_ids_for_commit(canon_commit_id).get("feedback", "")
        raise PostCanonMaintenanceBlocked(
            "post-Canon barrier blocks continuation: " + "; ".join(blockers),
            canon_commit_id=canon_commit_id,
            run_id=run_id,
            step_name="order_controls",
        )

    def run_for_chapter(
        self,
        *,
        project_id: str,
        chapter_number: int,
        worker_id: str,
        canon_commit_id: str = "",
    ) -> dict[str, Any]:
        commit_id = str(canon_commit_id or "").strip()
        if commit_id:
            with self.session_factory() as session:
                commit = session.get(CanonCommitRecord, commit_id)
                if (
                    commit is None
                    or commit.status != "committed"
                    or commit.project_id != str(project_id or "").strip()
                    or int(commit.chapter_number or 0) != int(chapter_number or 0)
                ):
                    raise ValueError(
                        "post-Canon commit does not match project/chapter context"
                    )
        else:
            commit_id = self.resolve_canon_commit(
                project_id=project_id,
                chapter_number=chapter_number,
            )
        return self.run(canon_commit_id=commit_id, worker_id=worker_id)

    def run(self, *, canon_commit_id: str, worker_id: str) -> dict[str, Any]:
        commit_id = str(canon_commit_id or "").strip()
        owner = str(worker_id or "").strip()
        if not commit_id:
            raise ValueError("canon_commit_id must be non-empty")
        if not owner:
            raise ValueError("worker_id must be non-empty")

        self._ensure_runs(commit_id)
        self._assert_previous_canon_ready(commit_id)
        run_ids: dict[str, str] = {}
        for step_name in POST_CANON_STEP_NAMES:
            claim, run_id = self._claim_step(
                canon_commit_id=commit_id,
                step_name=step_name,
                worker_id=owner,
            )
            run_ids[step_name] = run_id
            if claim is not None:
                self._execute_claim(claim)

        with self.session_factory() as session:
            commit = session.get(CanonCommitRecord, commit_id)
            if commit is None:
                raise ValueError(f"Canon commit not found: {commit_id}")
            return {
                "ok": True,
                "canon_commit_id": commit.id,
                "project_id": commit.project_id,
                "chapter_number": int(commit.chapter_number or 0),
                "candidate_id": commit.candidate_id,
                "run_ids": run_ids,
            }

    def run_order_controls(
        self,
        *,
        canon_commit_id: str,
        runner: OrderControlsRunner,
    ) -> dict[str, Any]:
        commit_id = str(canon_commit_id or "").strip()
        try:
            with self.session_factory.begin() as session:
                rows = self._locked_runs(session, commit_id)
                self._require_all_steps_succeeded(rows, commit_id)
                feedback = _row_for_step(rows, "feedback")
                payload = _load_object(feedback.result_json)
                controls = payload.get(ORDER_CONTROLS_KEY)
                if isinstance(controls, dict) and controls.get("status") == "succeeded":
                    result = controls.get("result")
                    return dict(result) if isinstance(result, dict) else {}

                commit = session.get(CanonCommitRecord, commit_id)
                if commit is None or commit.status != "committed":
                    raise ValueError(f"Committed Canon not found: {commit_id}")
                self._drain_llm_attempts()
                result = dict(runner(session, commit) or {})
                attempts = self._drain_llm_attempts()
                trace = self._enqueue_step_trace(
                    session=session,
                    commit=commit,
                    step_name="order_controls",
                    attempts=attempts,
                )
                if trace:
                    result["trace"] = trace
                json.dumps(result, ensure_ascii=False, sort_keys=True)
                blockers = _blocking_reasons(result)
                payload[ORDER_CONTROLS_KEY] = {
                    "status": "blocked" if blockers else "succeeded",
                    "continuation_blocked": bool(blockers),
                    "completed_at": self._clock().isoformat(),
                    "result": result,
                }
                feedback.result_json = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                session.add(feedback)
                return result
        except Exception as exc:
            self._discard_llm_attempts()
            self._record_order_controls_failure(commit_id, exc)
            if isinstance(exc, PostCanonMaintenanceError):
                raise
            feedback_run_id = self._run_id_for_step(commit_id, "feedback")
            raise PostCanonMaintenanceError(
                f"post-Canon order controls failed: {safe_error_summary(exc)}",
                canon_commit_id=commit_id,
                run_id=feedback_run_id,
                step_name="order_controls",
            ) from exc

    def heartbeat(self, claim: PostCanonRunClaim) -> None:
        timestamp = self._clock()
        with self.session_factory.begin() as session:
            updated = session.execute(
                update(PostCanonMaintenanceRun)
                .where(
                    *_claim_predicates(claim),
                    PostCanonMaintenanceRun.lease_expires_at.is_not(None),
                    PostCanonMaintenanceRun.lease_expires_at > timestamp,
                )
                .values(
                    heartbeat_at=timestamp,
                    lease_expires_at=timestamp + timedelta(seconds=claim.lease_seconds),
                )
            )
            if updated.rowcount != 1:
                raise _lease_lost(claim, "heartbeat")

    def _ensure_runs(self, canon_commit_id: str) -> None:
        with self.session_factory.begin() as session:
            commit = session.execute(
                select(CanonCommitRecord)
                .where(CanonCommitRecord.id == canon_commit_id)
                .with_for_update()
            ).scalar_one_or_none()
            if commit is None or commit.status != "committed":
                raise ValueError(f"Committed Canon not found: {canon_commit_id}")
            candidate = session.get(CandidateDraftRecord, commit.candidate_id)
            if (
                candidate is None
                or candidate.canon_commit_id != commit.id
                or candidate.status != "accepted"
            ):
                raise ValueError("Canon commit candidate is not accepted")
            existing = {
                row.step_name
                for row in session.execute(
                    select(PostCanonMaintenanceRun).where(
                        PostCanonMaintenanceRun.canon_commit_id == commit.id
                    )
                ).scalars()
            }
            unknown = sorted(existing - set(POST_CANON_STEP_NAMES))
            if unknown:
                raise ValueError(
                    "unknown post-Canon steps for committed Canon: "
                    + ", ".join(unknown)
                )
            for step_name in POST_CANON_STEP_NAMES:
                if step_name in existing:
                    continue
                session.add(
                    PostCanonMaintenanceRun(
                        id=new_id(),
                        canon_commit_id=commit.id,
                        project_id=commit.project_id,
                        chapter_number=int(commit.chapter_number or 0),
                        candidate_id=commit.candidate_id,
                        step_name=step_name,
                        idempotency_key=post_canon_idempotency_key(
                            commit.idempotency_key,
                            step_name,
                        ),
                        status="pending",
                    )
                )

    def _assert_previous_canon_ready(self, canon_commit_id: str) -> None:
        with self.session_factory() as session:
            commit = session.get(CanonCommitRecord, canon_commit_id)
            if commit is None:
                raise ValueError(f"Canon commit not found: {canon_commit_id}")
            previous = session.execute(
                select(CanonCommitRecord)
                .where(
                    CanonCommitRecord.project_id == commit.project_id,
                    CanonCommitRecord.chapter_number < commit.chapter_number,
                    CanonCommitRecord.status == "committed",
                )
                .order_by(CanonCommitRecord.chapter_number.desc())
                .limit(1)
            ).scalar_one_or_none()
            if previous is None:
                return
            rows = list(
                session.execute(
                    select(PostCanonMaintenanceRun).where(
                        PostCanonMaintenanceRun.canon_commit_id == previous.id
                    )
                ).scalars()
            )
            project = session.get(Project, commit.project_id)
            if project is None:
                raise ValueError(f"Project not found: {commit.project_id}")
            action = (
                ProjectPolicyStore(session)
                .load(project)
                .policy.pause.band_checkpoint_action
            )
            if not post_canon_barrier_ready(
                rows,
                session=session,
                band_checkpoint_action=action,
            ):
                feedback_run_id = next(
                    (row.id for row in rows if row.step_name == "feedback"),
                    "",
                )
                raise PostCanonMaintenanceBlocked(
                    f"previous Canon chapter {previous.chapter_number} has an "
                    "incomplete post-Canon barrier",
                    canon_commit_id=previous.id,
                    run_id=feedback_run_id,
                    step_name="order_controls",
                )

    def _claim_step(
        self,
        *,
        canon_commit_id: str,
        step_name: str,
        worker_id: str,
    ) -> tuple[PostCanonRunClaim | None, str]:
        timestamp = self._clock()
        with self.session_factory.begin() as session:
            row = session.execute(
                select(PostCanonMaintenanceRun)
                .where(
                    PostCanonMaintenanceRun.canon_commit_id == canon_commit_id,
                    PostCanonMaintenanceRun.step_name == step_name,
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if row is None:
                raise PostCanonMaintenanceBusy(
                    f"post-Canon step is locked: {step_name}",
                    canon_commit_id=canon_commit_id,
                    step_name=step_name,
                )
            if row.status == "succeeded":
                return None, row.id
            self._require_predecessors_succeeded(session, row)
            if row.status == "running" and not _lease_expired(
                row.lease_expires_at,
                timestamp,
            ):
                raise PostCanonMaintenanceBusy(
                    f"post-Canon step {step_name} is owned by a live worker",
                    canon_commit_id=canon_commit_id,
                    run_id=row.id,
                    step_name=step_name,
                )
            if row.available_at is not None and _datetime_after(
                row.available_at,
                timestamp,
            ):
                raise PostCanonMaintenanceBusy(
                    f"post-Canon step {step_name} is not available yet",
                    canon_commit_id=canon_commit_id,
                    run_id=row.id,
                    step_name=step_name,
                )

            row.status = "running"
            row.attempts = int(row.attempts or 0) + 1
            row.worker_id = worker_id
            row.lease_epoch = int(row.lease_epoch or 0) + 1
            row.heartbeat_at = timestamp
            row.lease_expires_at = timestamp + timedelta(seconds=self.lease_seconds)
            row.available_at = None
            row.last_error = ""
            row.started_at = timestamp
            row.completed_at = None
            session.add(row)
            session.flush()
            return (
                PostCanonRunClaim(
                    run_id=row.id,
                    canon_commit_id=row.canon_commit_id,
                    project_id=row.project_id,
                    chapter_number=int(row.chapter_number or 0),
                    candidate_id=row.candidate_id,
                    step_name=row.step_name,
                    worker_id=worker_id,
                    lease_epoch=int(row.lease_epoch or 0),
                    lease_seconds=self.lease_seconds,
                ),
                row.id,
            )

    def _execute_claim(self, claim: PostCanonRunClaim) -> None:
        runner = self._step_runners[claim.step_name]
        heartbeat = _ClaimHeartbeat(
            service=self,
            claim=claim,
            interval_seconds=self.heartbeat_interval_seconds,
        )
        try:
            self.heartbeat(claim)
            heartbeat.start()
            with self.session_factory.begin() as session:
                commit = session.get(CanonCommitRecord, claim.canon_commit_id)
                if commit is None or commit.status != "committed":
                    raise ValueError("maintenance Canon commit is unavailable")
                self._drain_llm_attempts()
                result = dict(runner(session, commit) or {})
                attempts = self._drain_llm_attempts()
                heartbeat.stop_and_join()
                if heartbeat.ownership_lost:
                    raise _lease_lost(claim, "complete")
                self._acquire_completion_fence(session, claim)
                trace = self._enqueue_step_trace(
                    session=session,
                    commit=commit,
                    step_name=claim.step_name,
                    attempts=attempts,
                )
                if trace:
                    result["trace"] = trace
                self._complete_claim_in_session(session, claim, result)
        except Exception as exc:
            heartbeat.stop_and_join()
            self._discard_llm_attempts()
            if not isinstance(exc, PostCanonLeaseLost):
                self._fail_claim(claim, exc)
            if isinstance(exc, PostCanonMaintenanceError):
                raise
            raise PostCanonMaintenanceError(
                f"post-Canon step {claim.step_name} failed: {safe_error_summary(exc)}",
                canon_commit_id=claim.canon_commit_id,
                run_id=claim.run_id,
                step_name=claim.step_name,
            ) from exc

    def _acquire_completion_fence(
        self,
        session: Session,
        claim: PostCanonRunClaim,
    ) -> None:
        timestamp = self._clock()
        updated = session.execute(
            update(PostCanonMaintenanceRun)
            .where(
                *_claim_predicates(claim),
                PostCanonMaintenanceRun.lease_expires_at.is_not(None),
                PostCanonMaintenanceRun.lease_expires_at > timestamp,
            )
            .values(
                heartbeat_at=timestamp,
                lease_expires_at=timestamp + timedelta(seconds=claim.lease_seconds),
            )
        )
        if updated.rowcount != 1:
            raise _lease_lost(claim, "acquire completion fence for")

    def _complete_claim_in_session(
        self,
        session: Session,
        claim: PostCanonRunClaim,
        result: Mapping[str, Any],
    ) -> None:
        encoded = json.dumps(dict(result), ensure_ascii=False, sort_keys=True)
        timestamp = self._clock()
        updated = session.execute(
            update(PostCanonMaintenanceRun)
            .where(*_claim_predicates(claim))
            .values(
                status="succeeded",
                result_json=encoded,
                last_error="",
                worker_id="",
                lease_expires_at=None,
                heartbeat_at=timestamp,
                completed_at=timestamp,
                available_at=None,
            )
        )
        if updated.rowcount != 1:
            raise _lease_lost(claim, "complete")

    def _fail_claim(self, claim: PostCanonRunClaim, exc: BaseException) -> None:
        error = safe_error_summary(exc)[:MAX_ERROR_LENGTH]
        timestamp = self._clock()
        with self.session_factory.begin() as session:
            updated = session.execute(
                update(PostCanonMaintenanceRun)
                .where(
                    *_claim_predicates(claim),
                    PostCanonMaintenanceRun.lease_expires_at.is_not(None),
                    PostCanonMaintenanceRun.lease_expires_at > timestamp,
                )
                .values(
                    status="failed",
                    last_error=error,
                    worker_id="",
                    lease_expires_at=None,
                    heartbeat_at=timestamp,
                    available_at=timestamp,
                    completed_at=None,
                )
            )
            if updated.rowcount != 1:
                raise _lease_lost(claim, "fail")

    def _require_predecessors_succeeded(
        self,
        session: Session,
        row: PostCanonMaintenanceRun,
    ) -> None:
        step_index = POST_CANON_STEP_NAMES.index(row.step_name)
        if step_index == 0:
            return
        expected = POST_CANON_STEP_NAMES[:step_index]
        predecessors = {
            item.step_name: item.status
            for item in session.execute(
                select(PostCanonMaintenanceRun).where(
                    PostCanonMaintenanceRun.canon_commit_id == row.canon_commit_id,
                    PostCanonMaintenanceRun.step_name.in_(expected),
                )
            ).scalars()
        }
        incomplete = [
            name for name in expected if predecessors.get(name) != "succeeded"
        ]
        if incomplete:
            raise PostCanonMaintenanceBlocked(
                f"post-Canon step {row.step_name} is blocked by "
                + ", ".join(incomplete),
                canon_commit_id=row.canon_commit_id,
                run_id=row.id,
                step_name=row.step_name,
            )

    def _locked_runs(
        self,
        session: Session,
        canon_commit_id: str,
    ) -> list[PostCanonMaintenanceRun]:
        return list(
            session.execute(
                select(PostCanonMaintenanceRun)
                .where(PostCanonMaintenanceRun.canon_commit_id == canon_commit_id)
                .order_by(PostCanonMaintenanceRun.created_at.asc())
                .with_for_update()
            ).scalars()
        )

    @staticmethod
    def _require_all_steps_succeeded(
        rows: list[PostCanonMaintenanceRun],
        canon_commit_id: str,
    ) -> None:
        status_by_step = {row.step_name: row.status for row in rows}
        if (
            len(rows) != len(POST_CANON_STEP_NAMES)
            or set(status_by_step) != set(POST_CANON_STEP_NAMES)
        ):
            raise PostCanonMaintenanceBlocked(
                "order controls require exactly the canonical steps: "
                + ", ".join(POST_CANON_STEP_NAMES),
                canon_commit_id=canon_commit_id,
                step_name="order_controls",
            )
        incomplete = [
            name
            for name in POST_CANON_STEP_NAMES
            if status_by_step.get(name) != "succeeded"
        ]
        if incomplete:
            raise PostCanonMaintenanceBlocked(
                "order controls require completed post-Canon steps: "
                + ", ".join(incomplete),
                canon_commit_id=canon_commit_id,
                step_name="order_controls",
            )

    def _record_order_controls_failure(
        self,
        canon_commit_id: str,
        exc: BaseException,
    ) -> None:
        error = safe_error_summary(exc)[:MAX_ERROR_LENGTH]
        with self.session_factory.begin() as session:
            feedback = session.execute(
                select(PostCanonMaintenanceRun)
                .where(
                    PostCanonMaintenanceRun.canon_commit_id == canon_commit_id,
                    PostCanonMaintenanceRun.step_name == "feedback",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if feedback is None or feedback.status != "succeeded":
                return
            payload = _load_object(feedback.result_json)
            controls = payload.get(ORDER_CONTROLS_KEY)
            if isinstance(controls, dict) and controls.get("status") == "succeeded":
                return
            payload[ORDER_CONTROLS_KEY] = {
                "status": "failed",
                "last_error": error,
                "failed_at": self._clock().isoformat(),
            }
            feedback.result_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            session.add(feedback)

    def _run_id_for_step(self, canon_commit_id: str, step_name: str) -> str:
        with self.session_factory() as session:
            return str(
                session.execute(
                    select(PostCanonMaintenanceRun.id).where(
                        PostCanonMaintenanceRun.canon_commit_id == canon_commit_id,
                        PostCanonMaintenanceRun.step_name == step_name,
                    )
                ).scalar_one_or_none()
                or ""
            )

    def _run_planning_step(
        self,
        session: Session,
        commit: CanonCommitRecord,
    ) -> Mapping[str, Any]:
        stage = self.stage_analyzer.analyze(
            session=session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
        )
        pacing = self.pacing_strategist.analyze(
            session=session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
        )
        analysis = save_stage_analysis(
            session=session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
            stage=stage,
            pacing=pacing,
        )
        replan = self.replan_governor.apply_if_needed(
            session=session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
            stage=stage,
            pacing=pacing,
        )
        return {
            "stage_analysis_id": analysis.id,
            "replan_event_id": str(getattr(replan, "id", "") or ""),
            "stage_label": stage.stage_label,
            "pacing_verdict": pacing.verdict,
        }

    def _run_arc_step(
        self,
        session: Session,
        commit: CanonCommitRecord,
    ) -> Mapping[str, Any]:
        envelope = self.arc_envelope_manager.ensure_active_arc_resolution(
            session=session,
            project_id=commit.project_id,
            activation_chapter=commit.chapter_number + 1,
        )
        promotion = self.arc_envelope_manager.record_provisional_promotion(
            session=session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
            reason="accepted-into-canon",
        )
        return {
            "arc_envelope_id": str(getattr(envelope, "id", "") or ""),
            "provisional_promotion_id": str(getattr(promotion, "id", "") or ""),
        }

    def _run_world_step(
        self,
        session: Session,
        commit: CanonCommitRecord,
    ) -> Mapping[str, Any]:
        turn = self.world_simulator.simulate(
            session=session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
        )
        save_world_turn(
            session=session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
            turn=turn,
        )
        return {
            "pressure_level": turn.pressure_level,
        }

    def _run_feedback_step(
        self,
        session: Session,
        commit: CanonCommitRecord,
    ) -> Mapping[str, Any]:
        result = run_feedback_aggregation_pass(
            session,
            commit.project_id,
            commit.chapter_number,
            cooldown_chapters=3,
            comment_to_reader_ratio=80,
        )
        return {
            "aggregate_count": len(result.all_aggregates),
            "actionable_count": len(result.actionable),
        }

    def _drain_llm_attempts(self) -> list[dict[str, Any]]:
        drain = getattr(self.llm_client, "drain_llm_attempt_events", None)
        attempts = drain() if callable(drain) else []
        return [dict(item) for item in attempts if isinstance(item, dict)]

    def _discard_llm_attempts(self) -> None:
        try:
            self._drain_llm_attempts()
        except Exception:  # noqa: BLE001
            return

    def _enqueue_step_trace(
        self,
        *,
        session: Session,
        commit: CanonCommitRecord,
        step_name: str,
        attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not attempts:
            return {}
        return enqueue_trace_upload(
            session,
            trace={
                "schema_version": "post-canon-trace-v1",
                "canon_commit_id": commit.id,
                "project_id": commit.project_id,
                "chapter_number": int(commit.chapter_number or 0),
                "step_name": step_name,
                "attempts": _safe_attempts(attempts),
            },
        )


def post_canon_idempotency_key(
    canon_idempotency_key: str,
    step_name: str,
) -> str:
    normalized_canon_key = str(canon_idempotency_key or "").strip()
    normalized_step = str(step_name or "").strip()
    if not normalized_canon_key or normalized_step not in POST_CANON_STEP_NAMES:
        raise ValueError("post-Canon idempotency identity is invalid")
    return f"post-canon-maintenance:v1:{normalized_canon_key}:{normalized_step}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _claim_predicates(claim: PostCanonRunClaim) -> tuple[Any, ...]:
    return (
        PostCanonMaintenanceRun.id == claim.run_id,
        PostCanonMaintenanceRun.status == "running",
        PostCanonMaintenanceRun.worker_id == claim.worker_id,
        PostCanonMaintenanceRun.lease_epoch == claim.lease_epoch,
    )


def _lease_lost(claim: PostCanonRunClaim, action: str) -> PostCanonLeaseLost:
    return PostCanonLeaseLost(
        f"stale post-Canon worker cannot {action} run {claim.run_id}",
        canon_commit_id=claim.canon_commit_id,
        run_id=claim.run_id,
        step_name=claim.step_name,
    )


def _row_for_step(
    rows: list[PostCanonMaintenanceRun],
    step_name: str,
) -> PostCanonMaintenanceRun:
    for row in rows:
        if row.step_name == step_name:
            return row
    raise ValueError(f"post-Canon run is missing step {step_name}")


def _load_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _safe_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe_keys = {
        "attempt_group_id",
        "attempt_no",
        "profile_id",
        "profile_name",
        "model",
        "provider",
        "stage_key",
        "http_status",
        "provider_request_id",
        "duration_ms",
        "input_chars",
        "output_chars",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "error_class",
        "error_category",
        "retryable",
        "fallback_eligible",
        "final_failure",
    }
    return [
        {
            str(key): value
            for key, value in attempt.items()
            if str(key) in safe_keys and value is not None
        }
        for attempt in attempts
    ]


def _blocking_reasons(result: Mapping[str, Any]) -> list[str]:
    raw = result.get("blocking_reasons")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _lease_expired(value: datetime | None, now: datetime) -> bool:
    return value is None or not _datetime_after(value, now)


def _datetime_after(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None and right.tzinfo is not None:
        right = right.replace(tzinfo=None)
    elif left.tzinfo is not None and right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return left > right


def _positive_seconds(value: float, name: str) -> float:
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


__all__ = [
    "PostCanonLeaseLost",
    "PostCanonMaintenanceBlocked",
    "PostCanonMaintenanceBusy",
    "PostCanonMaintenanceError",
    "PostCanonMaintenanceService",
    "PostCanonRunClaim",
    "post_canon_idempotency_key",
]
