from __future__ import annotations

from forwin.book_state.repository import BookStateRepository
from forwin.protocol.book_state import (
    GraphDelta,
    GraphDeltaType,
    NarrativeNode,
    WORLD_EDGE_TYPES_BY_FAMILY,
    WorldEdge,
    WorldNode,
)
import json
from forwin.generation.pipeline_core.common import logger
from forwin.protocol.review import ReviewVerdict
from forwin.models.draft import ChapterReview
from forwin.models import new_id
from forwin.generation.pipeline_core.result import RunResult
from sqlalchemy.orm import Session
from forwin.state.updater import StateUpdater
from forwin.protocol.writer import WriterOutput
def _load_review_verdict(review: ChapterReview) -> ReviewVerdict:
    meta = (
        json.loads(review.review_meta_json or "{}")
        if getattr(review, "review_meta_json", "")
        else {}
    )
    if not isinstance(meta, dict):
        meta = {}
    return ReviewVerdict.model_validate(
        {
            "verdict": review.verdict,
            "issues": json.loads(review.issues_json or "[]"),
            **meta,
        }
    )


class FinalizationStage:
    """Owns the finalization stage behavior."""

    def _flush_background_llm_trace(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        stage_key: str,
        trace_scope: str,
    ) -> str:
        drain_attempts = getattr(self.llm_client, "drain_llm_attempt_events", None)
        attempts = drain_attempts() if callable(drain_attempts) else []
        if not attempts:
            return ""
        return self._save_prompt_trace_payload(
            session=session,
            updater=StateUpdater(session),
            project_id=project_id,
            prompt_trace={
                "trace_scope": trace_scope,
                "stage_key": stage_key,
                "template_id": f"{trace_scope}:{stage_key}",
                "template_version": "v1",
                "effective_system_prompt": "",
                "prompt_layers": [],
                "input_snapshot": {
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "stage_key": stage_key,
                },
                "model_profile": {
                    "profile_id": getattr(self.llm_client, "profile_id", ""),
                    "profile_name": getattr(self.llm_client, "profile_name", ""),
                    "model": getattr(self.llm_client, "model", ""),
                    "base_url": getattr(self.llm_client, "base_url", ""),
                },
                "attempts": attempts,
                "output_summary": {
                    "status": "recorded",
                    "chapter_number": chapter_number,
                },
            },
        )
    def _abort_requested(self) -> bool:
        try:
            return bool(self.should_abort and self.should_abort())
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring abort predicate failure.", exc_info=True)
            return False

    def _pause_requested(self) -> bool:
        try:
            return bool(self.should_pause and self.should_pause())
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring pause predicate failure.", exc_info=True)
            return False

    def _paused_result(
        self,
        project_id: str,
        requested_chapters: int,
        *,
        completed_chapters: list[int] | None = None,
        failed_chapters: list[int] | None = None,
        paused_chapters: list[int] | None = None,
        frozen_artifacts: list[str] | None = None,
        current_chapter: int = 0,
    ) -> RunResult:
        self._emit_progress(
            "stage_changed",
            stage="paused",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=current_chapter,
            completed_chapters=completed_chapters or [],
            failed_chapters=failed_chapters or [],
            paused_chapters=paused_chapters or [],
            frozen_artifacts=frozen_artifacts or [],
        )
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=list(completed_chapters or []),
            failed_chapters=list(failed_chapters or []),
            paused_chapters=list(paused_chapters or []),
            frozen_artifacts=list(frozen_artifacts or []),
            paused=True,
        )

    def _cancelled_result(
        self,
        project_id: str,
        requested_chapters: int,
        *,
        completed_chapters: list[int] | None = None,
        failed_chapters: list[int] | None = None,
        paused_chapters: list[int] | None = None,
        frozen_artifacts: list[str] | None = None,
        current_chapter: int = 0,
    ) -> RunResult:
        self._emit_progress(
            "stage_changed",
            stage="cancelled",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=current_chapter,
            completed_chapters=completed_chapters or [],
            failed_chapters=failed_chapters or [],
            paused_chapters=paused_chapters or [],
            frozen_artifacts=frozen_artifacts or [],
        )
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=list(completed_chapters or []),
            failed_chapters=list(failed_chapters or []),
            paused_chapters=list(paused_chapters or []),
            frozen_artifacts=list(frozen_artifacts or []),
            cancelled=True,
        )

    def _load_writer_output_from_meta(self, meta_path: str) -> WriterOutput:
        payload = self.artifact_store.read_json(meta_path)
        return WriterOutput.model_validate(payload)

    def _seed_state(
        self,
        updater: StateUpdater,
        project_id: str,
        arc_plan: dict,
        num_chapters: int,
    ) -> None:
        """Seed the database with initial state from the arc plan."""
        chapters = arc_plan.get("chapters", [])
        raw_outlines = arc_plan.get("arc_outlines") or []
        normalized_outlines: list[dict[str, int | str]] = []
        cursor = 1
        for index, raw in enumerate(raw_outlines, start=1):
            if cursor > num_chapters or not isinstance(raw, dict):
                break
            raw_count = int(raw.get("chapter_count", 0) or 0)
            if raw_count <= 0:
                continue
            remaining = num_chapters - cursor + 1
            chapter_count = min(raw_count, remaining)
            if chapter_count <= 0:
                break
            chapter_start = cursor
            chapter_end = cursor + chapter_count - 1
            normalized_outlines.append(
                {
                    "arc_number": index,
                    "chapter_start": chapter_start,
                    "chapter_end": chapter_end,
                    "chapter_count": chapter_count,
                    "arc_synopsis": str(raw.get("arc_synopsis", "")).strip()
                    or str(arc_plan.get("arc_synopsis", "")).strip(),
                }
            )
            cursor = chapter_end + 1
        if not normalized_outlines:
            normalized_outlines = [
                {
                    "arc_number": 1,
                    "chapter_start": 1,
                    "chapter_end": num_chapters,
                    "chapter_count": num_chapters,
                    "arc_synopsis": str(arc_plan.get("arc_synopsis", "")).strip(),
                }
            ]
        elif cursor <= num_chapters:
            normalized_outlines.append(
                {
                    "arc_number": len(normalized_outlines) + 1,
                    "chapter_start": cursor,
                    "chapter_end": num_chapters,
                    "chapter_count": num_chapters - cursor + 1,
                    "arc_synopsis": f"后续弧线：第{cursor}章至第{num_chapters}章",
                }
            )

        first_arc = None
        for outline in normalized_outlines:
            chapter_start = int(outline.get("chapter_start", 1) or 1)
            chapter_end = int(
                outline.get("chapter_end", chapter_start) or chapter_start
            )
            chapter_count = max(
                1,
                int(outline.get("chapter_count", chapter_end - chapter_start + 1) or 1),
            )
            arc = updater.create_arc_plan(
                project_id=project_id,
                arc_synopsis=str(outline.get("arc_synopsis", "") or ""),
                version=1,
                status="active" if first_arc is None else "planned",
                arc_number=int(outline.get("arc_number", 1) or 1),
                chapter_start=chapter_start,
                chapter_end=chapter_end,
                planned_target_size=chapter_count,
                planned_soft_min=max(1, int(round(chapter_count * 0.85))),
                planned_soft_max=max(chapter_count, int(round(chapter_count * 1.20))),
            )
            if first_arc is None:
                first_arc = arc
            for chapter_number in range(chapter_start, chapter_end + 1):
                ch = (
                    chapters[chapter_number - 1]
                    if chapter_number - 1 < len(chapters)
                    else {}
                )
                updater.create_chapter_plan(
                    project_id=project_id,
                    arc_plan_id=arc.id,
                    chapter_number=ch.get("chapter_number", chapter_number),
                    title=ch.get("title", f"第{chapter_number}章"),
                    one_line=ch.get("one_line", ""),
                    goals=ch.get("goals", []),
                )

        # Entities: characters
        from forwin.characters.creation import CharacterCreationHelper
        from forwin.characters.models import CharacterCreationRequest

        character_helper = CharacterCreationHelper(updater.session)
        book_state = BookStateRepository(updater.session)
        entity_map: dict[str, str] = {}  # name -> canonical character id
        for char_data in arc_plan.get("characters", []):
            initial_state = char_data.get("initial_state", {})
            result = character_helper.create_character(
                CharacterCreationRequest(
                    project_id=project_id,
                    source="arc_plan_seed",
                    source_ref=str(char_data.get("source_ref") or ""),
                    name=char_data.get("name", "未命名"),
                    description=char_data.get("description", ""),
                    aliases=char_data.get("aliases", []),
                    importance=char_data.get("importance", 5),
                    created_at_chapter=0,
                    profile={
                        "role_hint": str(char_data.get("role_hint") or ""),
                        "role_archetype": str(
                            char_data.get("role_archetype")
                            or char_data.get("role_hint")
                            or ""
                        ),
                        "narrative_role": str(char_data.get("narrative_role") or ""),
                        "public_identity": str(char_data.get("public_identity") or ""),
                    },
                    state=initial_state if isinstance(initial_state, dict) else {},
                    personality_tags=list(char_data.get("personality_tags") or []),
                    audit_reason="arc plan seed character",
                )
            )
            entity_map[result.character_name] = result.character_id

        # Entities: locations
        for loc_data in arc_plan.get("locations", []):
            initial_state = loc_data.get("initial_state", {})
            node = WorldNode(
                id=new_id(),
                project_id=project_id,
                node_type="location",
                name=loc_data.get("name", "未命名"),
                description=loc_data.get("description", ""),
                aliases=list(loc_data.get("aliases", [])),
                importance=loc_data.get("importance", 5),
                created_at_chapter=0,
                state=initial_state if isinstance(initial_state, dict) else {},
                metadata={"source": "arc_plan_seed"},
            )
            book_state.create_world_node(node)
            if node.state:
                book_state.append_world_node_state(
                    project_id=project_id,
                    node_id=node.id,
                    node_type="location",
                    as_of_chapter=0,
                    state=node.state,
                )
            entity_map[node.name] = node.id

        # Entities: factions
        for fac_data in arc_plan.get("factions", []):
            initial_state = fac_data.get("initial_state", {})
            node = WorldNode(
                id=new_id(),
                project_id=project_id,
                node_type="faction",
                name=fac_data.get("name", "未命名"),
                description=fac_data.get("description", ""),
                aliases=list(fac_data.get("aliases", [])),
                importance=fac_data.get("importance", 5),
                created_at_chapter=0,
                state=initial_state if isinstance(initial_state, dict) else {},
                metadata={"source": "arc_plan_seed"},
            )
            book_state.create_world_node(node)
            if node.state:
                book_state.append_world_node_state(
                    project_id=project_id,
                    node_id=node.id,
                    node_type="faction",
                    as_of_chapter=0,
                    state=node.state,
                )
            entity_map[node.name] = node.id

        # Relations
        for rel_data in arc_plan.get("relations", []):
            source_name = rel_data.get("source_name", "")
            target_name = rel_data.get("target_name", "")
            source_id = entity_map.get(source_name)
            target_id = entity_map.get(target_name)
            if source_id and target_id:
                requested_type = str(rel_data.get("relation_type") or "ally_of")
                edge_family = next(
                    (
                        family
                        for family, edge_types in WORLD_EDGE_TYPES_BY_FAMILY.items()
                        if requested_type in edge_types
                    ),
                    "social",
                )
                edge_type = (
                    requested_type
                    if requested_type in WORLD_EDGE_TYPES_BY_FAMILY[edge_family]
                    else "ally_of"
                )
                book_state.create_world_edge(
                    WorldEdge(
                        id=new_id(),
                        project_id=project_id,
                        source_id=source_id,
                        target_id=target_id,
                        edge_type=edge_type,
                        edge_family=edge_family,
                        established_at_chapter=0,
                        metadata={
                            "description": rel_data.get("description", ""),
                            "source": "arc_plan_seed",
                            "requested_relation_type": requested_type,
                        },
                    )
                )
            else:
                logger.warning(
                    "Skipping relation %s -> %s: entity not found.",
                    source_name,
                    target_name,
                )

        # Plot threads
        for thread_data in arc_plan.get("plot_threads", []):
            book_state.create_narrative_node(
                NarrativeNode(
                    id=new_id(),
                    project_id=project_id,
                    node_type="plot_thread",
                    title=thread_data.get("name", ""),
                    status="active",
                    payload={
                        "description": thread_data.get("description", ""),
                        "priority": thread_data.get("priority", 2),
                        "beats": [],
                    },
                    metadata={"created_at_chapter": 0, "source": "arc_plan_seed"},
                )
            )

        self.subworld_manager.apply_initial_arc_plan(
            session=updater.session,
            project_id=project_id,
            arc_id=first_arc.id if first_arc is not None else "",
            arc_plan=arc_plan,
            entity_map=entity_map,
        )

        # Initial timeline
        initial_time = arc_plan.get("initial_time", {})
        if initial_time:
            delta_id = f"genesis_story_time:{project_id}"
            if not book_state.graph_delta_ids_exist([delta_id]):
                book_state.append_graph_delta(
                    GraphDelta(
                        id=delta_id,
                        project_id=project_id,
                        chapter_number=0,
                        story_time=initial_time.get("label", "故事开始"),
                        delta_type=GraphDeltaType.WORLD_STATE,
                        operation="seed_story_time",
                        target_type="project",
                        target_id=project_id,
                        source_type="genesis",
                        source_id="arc_plan_seed",
                        summary=initial_time.get("description", ""),
                    )
                )

    @staticmethod
    def _load_review_verdict(review: ChapterReview) -> ReviewVerdict:
        return _load_review_verdict(review)


__all__ = ["FinalizationStage"]
