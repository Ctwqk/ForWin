from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventInfo
from forwin.chapter_titles import rebase_generic_numeric_chapter_title
from forwin.models import (
    ArcPlanVersion,
    BandCheckpoint,
    BandExperiencePlan,
    BookGenesisRevision,
    ChapterDraft,
    ChapterPlan,
    ChapterReview,
    ChapterRewriteAttempt,
    DecisionEvent,
    NarrativeConstraint,
    Project,
    PromptTrace,
    SubWorld,
    SubWorldRosterItem,
    new_id,
)
from forwin.observability.redaction import redact_payload
from forwin.planning.checkpoints import BandCheckpointDetail
from forwin.planning.constraints import NarrativeConstraintInfo
from forwin.planning.contracts import (
    derive_band_task_contract,
    derive_chapter_task_contract,
    plan_task_contract_to_json,
)
from forwin.protocol import (
    BandDelightSchedule,
    ChapterExperiencePlan,
    ReviewVerdict,
    WriterOutput,
)
from forwin.review.issue_groups import issue_group_for_issue

if TYPE_CHECKING:
    from forwin.runtime.policy import RuntimePolicy

from forwin.protocol.review import normalize_repair_scope

from .repo import StateRepository

logger = logging.getLogger(__name__)

def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_goals_payload(payload: object) -> list[str]:
    if isinstance(payload, str):
        goal = payload.strip()
        return [goal] if len(goal) >= 2 else []
    if not isinstance(payload, list):
        return []
    return [str(item).strip() for item in payload if len(str(item).strip()) >= 2]


class StateUpdater:
    """Writes state changes to the database.

    Uses ``session.add()`` + ``session.flush()`` rather than ``session.commit()``
    so that the calling pipeline controls transaction boundaries.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self._repo = StateRepository(session)

    # ------------------------------------------------------------------
    # Project / Arc / Chapter
    # ------------------------------------------------------------------

    def create_project(
        self,
        title: str,
        premise: str,
        genre: str,
        setting_summary: str = "",
        target_total_chapters: int = 3,
        *,
        runtime_policy: RuntimePolicy,
        creation_status: str = "creating",
        active_genesis_revision_id: str = "",
        automation_json: str = "{}",
    ) -> Project:
        """Create a new project and flush to the session."""
        project = Project(
            id=new_id(),
            title=title,
            premise=premise,
            genre=genre,
            setting_summary=setting_summary,
            target_total_chapters=max(1, int(target_total_chapters or 1)),
            creation_status=str(creation_status or "creating").strip() or "creating",
            active_genesis_revision_id=str(active_genesis_revision_id or "").strip(),
            automation_json=str(automation_json or "{}"),
            runtime_policy_json=runtime_policy.model_dump_json(),
            runtime_policy_version=1,
        )
        self.session.add(project)
        self.session.flush()
        return project

    def create_arc_plan(
        self,
        project_id: str,
        arc_synopsis: str,
        version: int = 1,
        status: str = "active",
        arc_number: int = 1,
        chapter_start: int = 1,
        chapter_end: int = 0,
        planned_target_size: int = 0,
        planned_soft_min: int = 0,
        planned_soft_max: int = 0,
    ) -> ArcPlanVersion:
        """Create an arc plan version."""
        arc = ArcPlanVersion(
            id=new_id(),
            project_id=project_id,
            version=version,
            arc_number=max(1, int(arc_number or 1)),
            chapter_start=max(1, int(chapter_start or 1)),
            chapter_end=max(0, int(chapter_end or 0)),
            arc_synopsis=arc_synopsis,
            planned_target_size=max(0, int(planned_target_size or 0)),
            planned_soft_min=max(0, int(planned_soft_min or 0)),
            planned_soft_max=max(0, int(planned_soft_max or 0)),
            status=str(status or "active"),
        )
        self.session.add(arc)
        self.session.flush()
        return arc

    def create_chapter_plan(
        self,
        project_id: str,
        arc_plan_id: str,
        chapter_number: int,
        title: str,
        one_line: str,
        goals: list[str],
        experience_plan: ChapterExperiencePlan | None = None,
        task_contract=None,
    ) -> ChapterPlan:
        """Create a chapter plan."""
        normalized_goals = _normalize_goals_payload(goals)
        resolved_task_contract = task_contract or derive_chapter_task_contract(
            normalized_goals
        )
        plan = ChapterPlan(
            id=new_id(),
            project_id=project_id,
            arc_plan_id=arc_plan_id,
            chapter_number=chapter_number,
            title=rebase_generic_numeric_chapter_title(title, chapter_number),
            one_line=one_line,
            goals_json=json.dumps(normalized_goals, ensure_ascii=False),
            experience_plan_json=json.dumps(
                (experience_plan or ChapterExperiencePlan()).model_dump(mode="json"),
                ensure_ascii=False,
            ),
            task_contract_json=plan_task_contract_to_json(resolved_task_contract),
            status="planned",
        )
        self.session.add(plan)
        self.session.flush()
        return plan

    def create_book_genesis_revision(
        self,
        *,
        project_id: str,
        revision: int,
        pack_json: str,
        based_on_revision_id: str = "",
        status: str = "draft",
    ) -> BookGenesisRevision:
        row = BookGenesisRevision(
            id=new_id(),
            project_id=project_id,
            revision=max(1, int(revision or 1)),
            based_on_revision_id=str(based_on_revision_id or "").strip(),
            status=str(status or "draft").strip() or "draft",
            pack_json=str(pack_json or "{}"),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def save_prompt_trace(
        self,
        *,
        project_id: str,
        genesis_revision_id: str = "",
        decision_event_id: str = "",
        parent_trace_id: str = "",
        trace_scope: str = "genesis",
        stage_key: str = "",
        template_id: str = "",
        template_version: str = "v1",
        effective_system_prompt: str = "",
        prompt_layers_json: str = "[]",
        input_snapshot_json: str = "{}",
        model_profile_json: str = "{}",
        attempts_json: str = "[]",
        output_summary_json: str = "{}",
        backend: str = "",
        codex_job_id: str = "",
        permission_profile: str = "",
        fallback_used: bool = False,
    ) -> PromptTrace:
        row = PromptTrace(
            id=new_id(),
            project_id=project_id,
            genesis_revision_id=str(genesis_revision_id or "").strip(),
            decision_event_id=str(decision_event_id or "").strip(),
            parent_trace_id=str(parent_trace_id or "").strip(),
            trace_scope=str(trace_scope or "genesis").strip() or "genesis",
            stage_key=str(stage_key or "").strip(),
            template_id=str(template_id or "").strip(),
            template_version=str(template_version or "v1").strip() or "v1",
            effective_system_prompt=str(effective_system_prompt or ""),
            prompt_layers_json=str(prompt_layers_json or "[]"),
            input_snapshot_json=str(input_snapshot_json or "{}"),
            model_profile_json=str(model_profile_json or "{}"),
            attempts_json=str(attempts_json or "[]"),
            output_summary_json=str(output_summary_json or "{}"),
            backend=str(backend or ""),
            codex_job_id=str(codex_job_id or ""),
            permission_profile=str(permission_profile or ""),
            fallback_used=bool(fallback_used),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def update_chapter_experience_plan(
        self,
        project_id: str,
        chapter_number: int,
        experience_plan: ChapterExperiencePlan,
    ) -> ChapterPlan | None:
        plan = self._repo.get_chapter_plan(project_id, chapter_number)
        if plan is None:
            return None
        plan.experience_plan_json = json.dumps(
            experience_plan.model_dump(mode="json"),
            ensure_ascii=False,
        )
        self.session.add(plan)
        self.session.flush()
        return plan

    def create_subworld(
        self,
        *,
        project_id: str,
        origin_arc_id: str | None,
        parent_subworld_id: str | None,
        name: str,
        purpose: str,
        scope: str,
        status: str = "active",
        introduced_at_chapter: int = 0,
        retired_at_chapter: int | None = None,
        metadata: dict | None = None,
    ) -> SubWorld:
        row = SubWorld(
            id=new_id(),
            project_id=project_id,
            origin_arc_id=origin_arc_id,
            parent_subworld_id=parent_subworld_id,
            name=name,
            purpose=purpose,
            scope=scope,
            status=status,
            introduced_at_chapter=int(introduced_at_chapter or 0),
            retired_at_chapter=retired_at_chapter,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_roster_item(
        self,
        *,
        project_id: str,
        subworld_id: str,
        entity_id: str | None,
        entity_kind: str = "character",
        display_name: str = "",
        slot_key: str = "",
        role_hint: str = "",
        description: str = "",
        is_core: bool = False,
        status: str = "planned_slot",
        activation_chapter: int = 0,
        metadata: dict | None = None,
    ) -> SubWorldRosterItem:
        row = SubWorldRosterItem(
            id=new_id(),
            project_id=project_id,
            subworld_id=subworld_id,
            entity_id=entity_id,
            entity_kind=entity_kind,
            display_name=display_name,
            slot_key=slot_key,
            role_hint=role_hint,
            description=description,
            is_core=is_core,
            status=status,
            activation_chapter=int(activation_chapter or 0),
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        self.session.add(row)
        self.session.flush()
        return row

    # ------------------------------------------------------------------
    # Draft / Review
    # ------------------------------------------------------------------

    def save_draft(
        self,
        chapter_plan_id: str,
        writer_output: WriterOutput,
        raw_response: str,
        model_name: str = "",
    ) -> ChapterDraft:
        """Save a chapter draft.

        Determines the next version number by checking existing drafts for the
        same chapter_plan_id.
        """
        # Determine the next version number.
        existing_stmt = (
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == chapter_plan_id)
            .order_by(ChapterDraft.version.desc())
            .limit(1)
        )
        latest_draft = self.session.execute(existing_stmt).scalar_one_or_none()
        next_version = (latest_draft.version + 1) if latest_draft is not None else 1

        char_count = writer_output.char_count or len(writer_output.body)

        draft = ChapterDraft(
            id=new_id(),
            chapter_plan_id=chapter_plan_id,
            version=next_version,
            body_text=writer_output.body,
            summary=writer_output.end_of_chapter_summary,
            char_count=char_count,
            llm_model=model_name,
            llm_raw_response=raw_response,
        )
        self.session.add(draft)
        self.session.flush()
        return draft

    def save_review(
        self,
        draft_id: str,
        verdict: ReviewVerdict,
    ) -> ChapterReview:
        """Save a review verdict."""
        issues_data = []
        for issue in verdict.issues:
            payload = issue.model_dump()
            if not str(payload.get("issue_group") or "").strip():
                payload["issue_group"] = issue_group_for_issue(
                    issue_type=str(payload.get("issue_type") or ""),
                    rule_name=str(payload.get("rule_name") or ""),
                )
            issues_data.append(payload)
        review_meta = verdict.model_dump(mode="json")
        review_meta.pop("verdict", None)
        review_meta.pop("issues", None)
        review = ChapterReview(
            id=new_id(),
            draft_id=draft_id,
            verdict=verdict.verdict,
            issues_json=json.dumps(issues_data, ensure_ascii=False),
            review_meta_json=json.dumps(review_meta, ensure_ascii=False),
        )
        self.session.add(review)
        self.session.flush()
        return review

    def save_band_experience_plan(
        self,
        *,
        project_id: str,
        arc_id: str,
        schedule: BandDelightSchedule,
        task_contract=None,
    ) -> BandExperiencePlan:
        resolved_task_contract = task_contract or derive_band_task_contract(schedule)
        self.session.query(BandExperiencePlan).filter(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.arc_id == arc_id,
            BandExperiencePlan.band_id == schedule.band_id,
        ).delete(synchronize_session=False)
        row = BandExperiencePlan(
            id=new_id(),
            project_id=project_id,
            arc_id=arc_id,
            band_id=schedule.band_id,
            chapter_start=schedule.chapter_start,
            chapter_end=schedule.chapter_end,
            stall_guard_max_gap=schedule.stall_guard_max_gap,
            schedule_json=json.dumps(
                schedule.model_dump(mode="json"), ensure_ascii=False
            ),
            task_contract_json=plan_task_contract_to_json(resolved_task_contract),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def save_band_checkpoint(
        self,
        detail: BandCheckpointDetail,
        *,
        related_task_id: str = "",
    ) -> BandCheckpoint:
        row = BandCheckpoint(
            id=detail.id or new_id(),
            project_id=detail.project_id,
            arc_id=detail.arc_id,
            band_id=detail.band_id,
            chapter_start=detail.chapter_start,
            chapter_end=detail.chapter_end,
            trigger_source=detail.trigger_source,
            boundary_kind=detail.boundary_kind,
            boundary_chapter=detail.boundary_chapter,
            status=detail.status,
            summary=detail.summary,
            reason=detail.reason,
            issues_json=json.dumps(
                [
                    {
                        **issue.model_dump(mode="json"),
                        "issue_group": issue.issue_group
                        or issue_group_for_issue(code=issue.code),
                    }
                    for issue in detail.issues
                ],
                ensure_ascii=False,
            ),
            related_task_id=related_task_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def update_band_checkpoint(
        self,
        checkpoint_id: str,
        *,
        status: str | None = None,
        summary: str | None = None,
        reason: str | None = None,
        issues: list[dict[str, object]] | None = None,
    ) -> BandCheckpoint | None:
        row = self.session.get(BandCheckpoint, checkpoint_id)
        if row is None:
            return None
        if status is not None:
            row.status = status
        if summary is not None:
            row.summary = summary
        if reason is not None:
            row.reason = reason
        if issues is not None:
            row.issues_json = json.dumps(issues, ensure_ascii=False)
        self.session.add(row)
        self.session.flush()
        return row

    def save_narrative_constraint(
        self,
        info: NarrativeConstraintInfo,
    ) -> NarrativeConstraint:
        row = NarrativeConstraint(
            id=info.id or new_id(),
            project_id=info.project_id,
            arc_id=info.arc_id,
            band_id=info.band_id,
            constraint_type=info.constraint_type,
            level=info.level,
            subject_name=info.subject_name,
            description=info.description,
            payload_json=json.dumps(info.payload, ensure_ascii=False),
            effective_from_chapter=info.effective_from_chapter,
            protect_until_chapter=info.protect_until_chapter,
            status=info.status,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def update_narrative_constraint(
        self,
        constraint_id: str,
        info: NarrativeConstraintInfo,
    ) -> NarrativeConstraint | None:
        row = self.session.get(NarrativeConstraint, constraint_id)
        if row is None:
            return None
        row.arc_id = info.arc_id
        row.band_id = info.band_id
        row.constraint_type = info.constraint_type
        row.level = info.level
        row.subject_name = info.subject_name
        row.description = info.description
        row.payload_json = json.dumps(info.payload, ensure_ascii=False)
        row.effective_from_chapter = info.effective_from_chapter
        row.protect_until_chapter = info.protect_until_chapter
        row.status = info.status
        self.session.add(row)
        self.session.flush()
        return row

    def save_decision_event(
        self,
        info: DecisionEventInfo,
    ) -> DecisionEvent:
        payload = redact_payload(info.payload)
        row = DecisionEvent(
            id=info.id or new_id(),
            project_id=info.project_id,
            task_id=info.task_id,
            band_id=info.band_id,
            chapter_number=info.chapter_number,
            scope=info.scope,
            event_family=info.event_family,
            event_type=info.event_type,
            actor_type=info.actor_type,
            actor_id=info.actor_id,
            summary=info.summary,
            reason=info.reason,
            payload_json=json.dumps(payload, ensure_ascii=False),
            related_object_type=info.related_object_type,
            related_object_id=info.related_object_id,
            parent_event_id=info.parent_event_id,
            causal_root_id=info.causal_root_id,
        )
        self.session.add(row)
        self.session.flush()
        if not str(row.causal_root_id or "").strip():
            row.causal_root_id = row.id
            self.session.add(row)
            self.session.flush()
        return row

    def save_chapter_rewrite_attempt(
        self,
        *,
        project_id: str,
        chapter_number: int,
        attempt_no: int,
        repair_phase: str = "review_repair",
        phase_attempt_no: int | None = None,
        trigger_review_id: str,
        repair_scope: str,
        design_patch: dict[str, object],
        source_draft_id: str,
        result_draft_id: str,
        result_verdict: str,
        result_review_id: str = "",
        failure_reason: str = "",
        verification: dict[str, object] | None = None,
        source_chapter_plan: dict[str, object] | None = None,
        result_chapter_plan: dict[str, object] | None = None,
        source_band_plan: dict[str, object] | None = None,
        result_band_plan: dict[str, object] | None = None,
        forced_accept_applied: bool,
    ) -> ChapterRewriteAttempt:
        row = ChapterRewriteAttempt(
            id=new_id(),
            project_id=project_id,
            chapter_number=chapter_number,
            attempt_no=attempt_no,
            repair_phase=str(repair_phase or "review_repair"),
            phase_attempt_no=max(
                0, int(phase_attempt_no if phase_attempt_no is not None else attempt_no)
            ),
            trigger_review_id=trigger_review_id,
            repair_scope=normalize_repair_scope(repair_scope),
            design_patch_json=json.dumps(design_patch, ensure_ascii=False),
            source_draft_id=source_draft_id,
            result_draft_id=result_draft_id,
            result_verdict=result_verdict,
            result_review_id=result_review_id,
            failure_reason=str(failure_reason or ""),
            verification_json=json.dumps(verification or {}, ensure_ascii=False),
            source_chapter_plan_json=json.dumps(
                source_chapter_plan or {}, ensure_ascii=False
            ),
            result_chapter_plan_json=json.dumps(
                result_chapter_plan or {}, ensure_ascii=False
            ),
            source_band_plan_json=json.dumps(
                source_band_plan or {}, ensure_ascii=False
            ),
            result_band_plan_json=json.dumps(
                result_band_plan or {}, ensure_ascii=False
            ),
            forced_accept_applied=forced_accept_applied,
        )
        self.session.add(row)
        self.session.flush()
        return row

    # ------------------------------------------------------------------
    # Chapter status
    # ------------------------------------------------------------------

    def mark_chapter_status(
        self,
        project_id: str,
        chapter_number: int,
        status: str,
        *,
        acceptance_mode: str | None = None,
        repair_attempt_count: int | None = None,
        residual_review_issues: list[dict[str, object]] | None = None,
        canon_risk_level: str | None = None,
    ) -> None:
        """Update the status field on a ChapterPlan row."""
        self.session.scalar(select(Project).where(Project.id == project_id).with_for_update())
        plan = self.session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == chapter_number
        ).with_for_update().execution_options(populate_existing=True))
        if plan is None:
            logger.warning(
                "Cannot mark status: no chapter plan found for project=%s chapter=%d.",
                project_id,
                chapter_number,
            )
            return
        if plan.active_commit_id:
            if status != "accepted":
                raise ValueError("accepted chapter requires a candidate revision; cannot reset active acceptance")
            return  # Canon already wrote its acceptance metadata atomically.
        plan.title = rebase_generic_numeric_chapter_title(plan.title, chapter_number)
        plan.status = status
        if acceptance_mode is not None:
            plan.acceptance_mode = str(acceptance_mode or "")
        if repair_attempt_count is not None:
            plan.repair_attempt_count = max(0, int(repair_attempt_count or 0))
        if residual_review_issues is not None:
            plan.residual_review_issues_json = json.dumps(
                residual_review_issues,
                ensure_ascii=False,
            )
        if canon_risk_level is not None:
            plan.canon_risk_level = str(canon_risk_level or "")
        self.session.add(plan)
        self.session.flush()
