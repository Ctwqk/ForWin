from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import (
    ArcPlanVersion,
    CanonEvent,
    ChapterDraft,
    ChapterPlan,
    ChapterReview,
    ChapterTimeline,
    Entity,
    EntityAlias,
    EntityState,
    EventEntityLink,
    PlotThread,
    PlotThreadBeat,
    Project,
    RelationEdge,
    StoryTimePoint,
    new_id,
)
from forwin.protocol import (
    EventCandidate,
    ReviewVerdict,
    StateChangeCandidate,
    ThreadBeatCandidate,
    TimeAdvance,
    WriterOutput,
)

from .repo import StateRepository
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
    ) -> Project:
        """Create a new project and flush to the session."""
        project = Project(
            id=new_id(),
            title=title,
            premise=premise,
            genre=genre,
            setting_summary=setting_summary,
        )
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
    ) -> ChapterPlan:
        """Create a chapter plan."""
        plan = ChapterPlan(
            id=new_id(),
            project_id=project_id,
            arc_plan_id=arc_plan_id,
            chapter_number=chapter_number,
            title=title,
            one_line=one_line,
            goals_json=json.dumps(goals, ensure_ascii=False),
            status="planned",
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

            # Retrieve the most recent EntityState.
            stmt = (
                select(EntityState)
                .where(EntityState.entity_id == entity.id)
                .order_by(EntityState.as_of_chapter.desc())
                .limit(1)
            )
            latest_state = self.session.execute(stmt).scalar_one_or_none()

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
        issues_data = [issue.model_dump() for issue in verdict.issues]
        review = ChapterReview(
            id=new_id(),
            draft_id=draft_id,
            verdict=verdict.verdict,
            issues_json=json.dumps(issues_data, ensure_ascii=False),
        )
        self.session.add(review)
        self.session.flush()
        return review

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
