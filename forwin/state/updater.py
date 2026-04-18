from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.governance import (
    BandCheckpointDetail,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    derive_band_task_contract,
    derive_chapter_task_contract,
    governance_to_json,
    issue_group_for_issue,
    new_project_governance,
    plan_task_contract_to_json,
)
from forwin.models import (
    ArcPlanVersion,
    BandCheckpoint,
    BandExperiencePlan,
    CanonEvent,
    ChapterDraft,
    ChapterPlan,
    ChapterRewriteAttempt,
    ChapterReview,
    ChapterTimeline,
    DecisionEvent,
    Entity,
    EntityAlias,
    EntityState,
    EventEntityLink,
    NarrativeConstraint,
    PlotThread,
    PlotThreadBeat,
    Project,
    RelationEdge,
    StoryTimePoint,
    new_id,
)
from forwin.protocol import (
    BandDelightSchedule,
    ChapterExperiencePlan,
    EventCandidate,
    ReviewVerdict,
    StateChangeCandidate,
    ThreadBeatCandidate,
    TimeAdvance,
    WriterOutput,
)

from .repo import StateRepository
from .query_helpers import load_latest_entity_states
from .schema import prepare_state_change, validate_state_payload

logger = logging.getLogger(__name__)


class StateUpdater:
    """Writes state changes to the database.

    Uses ``session.add()`` + ``session.flush()`` rather than ``session.commit()``
    so that the calling orchestrator controls transaction boundaries.
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
        governance=None,
    ) -> Project:
        """Create a new project and flush to the session."""
        resolved_governance = governance or new_project_governance()
        project = Project(
            id=new_id(),
            title=title,
            premise=premise,
            genre=genre,
            setting_summary=setting_summary,
            target_total_chapters=max(1, int(target_total_chapters or 1)),
            governance_json=governance_to_json(resolved_governance),
        )
        self.session.add(project)
        self.session.flush()
        return project

    def update_project_governance(
        self,
        project_id: str,
        governance,
    ) -> Project | None:
        project = self._repo.get_project(project_id)
        if project is None:
            return None
        project.governance_json = governance_to_json(governance)
        self.session.add(project)
        self.session.flush()
        return project

    def create_arc_plan(
        self,
        project_id: str,
        arc_synopsis: str,
        version: int = 1,
    ) -> ArcPlanVersion:
        """Create an arc plan version."""
        arc = ArcPlanVersion(
            id=new_id(),
            project_id=project_id,
            version=version,
            arc_synopsis=arc_synopsis,
            status="active",
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
        resolved_task_contract = task_contract or derive_chapter_task_contract(goals)
        plan = ChapterPlan(
            id=new_id(),
            project_id=project_id,
            arc_plan_id=arc_plan_id,
            chapter_number=chapter_number,
            title=title,
            one_line=one_line,
            goals_json=json.dumps(goals, ensure_ascii=False),
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

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def create_entity(
        self,
        project_id: str,
        kind: str,
        name: str,
        description: str,
        aliases: Optional[list[str]] = None,
        importance: int = 5,
        chapter: int = 0,
    ) -> Entity:
        """Create a new entity."""
        entity = Entity(
            id=new_id(),
            project_id=project_id,
            kind=kind,
            name=name,
            description=description,
            aliases_json=json.dumps(aliases or [], ensure_ascii=False),
            importance=importance,
            created_at_chapter=chapter,
            is_active=True,
        )
        self.session.add(entity)
        for alias in aliases or []:
            alias_text = str(alias).strip()
            if not alias_text:
                continue
            self.session.add(
                EntityAlias(
                    id=new_id(),
                    entity_id=entity.id,
                    project_id=project_id,
                    alias=alias_text,
                )
            )
        self.session.flush()
        return entity

    def create_entity_state(
        self,
        entity_id: str,
        chapter: int,
        state: dict,
    ) -> EntityState:
        """Create a new entity state snapshot."""
        entity = self.session.get(Entity, entity_id)
        if entity is None:
            raise ValueError(f"Entity {entity_id} not found.")

        validated_state = validate_state_payload(entity.kind, state)
        es = EntityState(
            id=new_id(),
            entity_id=entity_id,
            as_of_chapter=chapter,
            state_json=json.dumps(validated_state, ensure_ascii=False),
        )
        self.session.add(es)
        self.session.flush()
        return es

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def create_relation(
        self,
        project_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
        description: str = "",
        chapter: int = 0,
    ) -> RelationEdge:
        """Create a relationship edge."""
        edge = RelationEdge(
            id=new_id(),
            project_id=project_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            relation_type=relation_type,
            description=description,
            established_at_chapter=chapter,
            ended_at_chapter=None,
            is_active=True,
        )
        self.session.add(edge)
        self.session.flush()
        return edge

    # ------------------------------------------------------------------
    # Plot Threads
    # ------------------------------------------------------------------

    def create_thread(
        self,
        project_id: str,
        name: str,
        description: str,
        priority: int = 2,
        chapter: int = 0,
    ) -> PlotThread:
        """Create a plot thread."""
        thread = PlotThread(
            id=new_id(),
            project_id=project_id,
            name=name,
            description=description,
            status="active",
            priority=priority,
            opened_at_chapter=chapter,
            closed_at_chapter=None,
        )
        self.session.add(thread)
        self.session.flush()
        return thread

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    def create_time_point(
        self,
        project_id: str,
        label: str,
        ordinal: int,
        description: str = "",
    ) -> StoryTimePoint:
        """Create a story time point."""
        stp = StoryTimePoint(
            id=new_id(),
            project_id=project_id,
            label=label,
            ordinal=ordinal,
            description=description,
        )
        self.session.add(stp)
        self.session.flush()
        return stp

    # ------------------------------------------------------------------
    # Applying writer output
    # ------------------------------------------------------------------

    def apply_state_changes(
        self,
        project_id: str,
        chapter_number: int,
        changes: list[StateChangeCandidate],
    ) -> None:
        """Apply state changes from writer output.

        For each change:
          1. Resolve entity_name to entity (via repo).
          2. If not found, create a new entity with kind=change.entity_kind.
          3. Get the latest EntityState for this entity.
          4. Parse state_json, update the specified field.
          5. Create a new EntityState with as_of_chapter=chapter_number.
        """
        entity_lookup = self._repo.get_entities_by_names(
            project_id,
            [change.entity_name for change in changes],
        )
        existing_entities = [entity for entity in entity_lookup.values() if entity is not None]
        latest_state_map = load_latest_entity_states(
            self.session,
            [entity.id for entity in existing_entities],
        )

        for change in changes:
            entity = entity_lookup.get(change.entity_name)

            if entity is None:
                logger.info(
                    "Entity '%s' not found; creating new %s entity.",
                    change.entity_name,
                    change.entity_kind,
                )
                entity = self.create_entity(
                    project_id=project_id,
                    kind=change.entity_kind,
                    name=change.entity_name,
                    description="",
                    chapter=chapter_number,
                )
                entity_lookup[change.entity_name] = entity

            latest_state = latest_state_map.get(entity.id)

            current_state: dict = {}
            if latest_state is not None:
                try:
                    current_state = json.loads(latest_state.state_json) or {}
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to parse state_json for entity %s; starting fresh.",
                        entity.id,
                    )

            _, next_state = prepare_state_change(
                kind=entity.kind,
                current_state=current_state,
                field=change.field,
                new_value=change.new_value,
            )

            self.create_entity_state(
                entity_id=entity.id,
                chapter=chapter_number,
                state=next_state,
            )
            latest_state_map[entity.id] = EntityState(
                entity_id=entity.id,
                as_of_chapter=chapter_number,
                state_json=json.dumps(next_state, ensure_ascii=False),
            )

    def apply_events(
        self,
        project_id: str,
        chapter_number: int,
        events: list[EventCandidate],
    ) -> None:
        """Apply events from writer output.

        For each event:
          1. Resolve every involved entity name to an entity ID.
          2. Abort the event if any entity cannot be resolved.
          3. Create a CanonEvent and all corresponding EventEntityLink rows.
        """
        entity_lookup = self._repo.get_entities_by_names(
            project_id,
            [
                entity_name
                for event_candidate in events
                for entity_name in event_candidate.involved_entity_names
            ],
        )
        for event_candidate in events:
            resolved_links: list[tuple[str, str]] = []
            names = event_candidate.involved_entity_names
            roles = event_candidate.roles

            for idx, entity_name in enumerate(names):
                role = roles[idx] if idx < len(roles) else "mentioned"
                entity = entity_lookup.get(entity_name)
                if entity is None:
                    raise ValueError(
                        "Event references unknown entity "
                        f"'{entity_name}' in chapter {chapter_number}."
                    )

                resolved_links.append((entity.id, role))

            canon_event = CanonEvent(
                id=new_id(),
                project_id=project_id,
                chapter_number=chapter_number,
                summary=event_candidate.summary,
                significance=event_candidate.significance,
            )
            self.session.add(canon_event)
            self.session.flush()

            for entity_id, role in resolved_links:
                link = EventEntityLink(
                    id=new_id(),
                    event_id=canon_event.id,
                    entity_id=entity_id,
                    role=role,
                )
                self.session.add(link)

            self.session.flush()

    def apply_thread_beats(
        self,
        project_id: str,
        chapter_number: int,
        beats: list[ThreadBeatCandidate],
    ) -> None:
        """Apply thread beats from writer output.

        For each beat:
          1. Resolve thread_name to a PlotThread.
          2. If not found, create a new thread.
          3. Create a PlotThreadBeat.
          4. If beat_type is "resolution", update thread status to "resolved".
        """
        for beat in beats:
            thread = self._repo.get_thread_by_name(project_id, beat.thread_name)

            if thread is None:
                logger.info(
                    "Thread '%s' not found; creating new thread.", beat.thread_name
                )
                thread = self.create_thread(
                    project_id=project_id,
                    name=beat.thread_name,
                    description="",
                    chapter=chapter_number,
                )

            ptb = PlotThreadBeat(
                id=new_id(),
                thread_id=thread.id,
                chapter_number=chapter_number,
                beat_type=beat.beat_type,
                description=beat.description,
            )
            self.session.add(ptb)

            if beat.beat_type == "resolution":
                thread.status = "resolved"
                thread.closed_at_chapter = chapter_number
                self.session.add(thread)

        self.session.flush()

    def apply_time_advance(
        self,
        project_id: str,
        chapter_number: int,
        advance: TimeAdvance,
    ) -> None:
        """Apply time advancement.

        1. Determine the current maximum ordinal.
        2. Create a new StoryTimePoint with ordinal+1.
        3. Create a ChapterTimeline linking the chapter to the new time point.
        """
        # Find the current max ordinal.
        stmt = (
            select(StoryTimePoint)
            .where(StoryTimePoint.project_id == project_id)
            .order_by(StoryTimePoint.ordinal.desc())
            .limit(1)
        )
        current_stp = self.session.execute(stmt).scalar_one_or_none()
        current_ordinal = current_stp.ordinal if current_stp is not None else 0
        new_ordinal = current_ordinal + 1

        new_stp = self.create_time_point(
            project_id=project_id,
            label=advance.new_time_label,
            ordinal=new_ordinal,
            description=advance.duration_description,
        )

        # Determine the start_time_id: use the previous time point if available.
        start_time_id = current_stp.id if current_stp is not None else new_stp.id

        timeline = ChapterTimeline(
            id=new_id(),
            project_id=project_id,
            chapter_number=chapter_number,
            start_time_id=start_time_id,
            end_time_id=new_stp.id,
            duration_description=advance.duration_description,
        )
        self.session.add(timeline)
        self.session.flush()

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
        review_meta = verdict.model_dump(mode="json", exclude_none=True)
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
            schedule_json=json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False),
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
            payload_json=json.dumps(info.payload, ensure_ascii=False),
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
        trigger_review_id: str,
        repair_scope: str,
        design_patch: dict[str, object],
        source_draft_id: str,
        result_draft_id: str,
        result_verdict: str,
        forced_accept_applied: bool,
    ) -> ChapterRewriteAttempt:
        row = ChapterRewriteAttempt(
            id=new_id(),
            project_id=project_id,
            chapter_number=chapter_number,
            attempt_no=attempt_no,
            trigger_review_id=trigger_review_id,
            repair_scope=repair_scope,
            design_patch_json=json.dumps(design_patch, ensure_ascii=False),
            source_draft_id=source_draft_id,
            result_draft_id=result_draft_id,
            result_verdict=result_verdict,
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
    ) -> None:
        """Update the status field on a ChapterPlan row."""
        plan = self._repo.get_chapter_plan(project_id, chapter_number)
        if plan is None:
            logger.warning(
                "Cannot mark status: no chapter plan found for project=%s chapter=%d.",
                project_id,
                chapter_number,
            )
            return
        plan.status = status
        self.session.add(plan)
        self.session.flush()
