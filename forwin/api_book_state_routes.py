from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schemas import (
    CharacterPersonalityActiveContextPreviewRequest,
    CharacterCreateRequest,
    CharacterPersonalityPreviewRequest,
    CharacterPersonalityReassignRequest,
    PersonalityLoadoutUpdateRequest,
)
from forwin.book_state import BookStateProjection, BookStateRepository
from forwin.characters.creation import CharacterCreationHelper
from forwin.characters.models import CharacterCreationRequest
from forwin.models.project import Project
from forwin.personality.assignment import PersonalityLoadoutAssigner
from forwin.personality.context import build_active_personality_context
from forwin.personality.enrichment import RelationshipPersonalityEnricher
from forwin.personality.library import CharacterPersonalityLibrary
from forwin.personality.metrics import build_character_personality_metrics
from forwin.personality.models import CharacterPersonalityPolicy, PersonalityAssignmentRequest, PersonalityLoadout
from forwin.personality.policy import CharacterPersonalityPolicyResolver
from forwin.personality.reports import PersonalityAssignmentReportStore
from forwin.state.updater import StateUpdater


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def build_handlers(
    *,
    get_session: Callable[[], Any],
    personality_library_root: str | None = None,
) -> dict[str, Callable[..., Any]]:
    def _personality_library() -> CharacterPersonalityLibrary:
        return CharacterPersonalityLibrary(personality_library_root)

    def _resolve_as_of_chapter(session, project_id: str, as_of_chapter: int | None = None) -> int:
        if as_of_chapter is not None and int(as_of_chapter or 0) > 0:
            return int(as_of_chapter)
        return BookStateRepository(session).latest_available_chapter(project_id)

    def get_book_state_runtime(project_id: str, as_of_chapter: int | None = None) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            resolved_chapter = (
                BookStateRepository(session).latest_available_chapter(project_id)
                if as_of_chapter is None
                else int(as_of_chapter)
            )
            runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=resolved_chapter)
            return {
                "schema_version": "book_state.runtime.v1",
                "project_id": project_id,
                "as_of_chapter": resolved_chapter,
                "world_node_count": len(runtime.world.nodes_by_id),
                "world_edge_count": len(runtime.world.edges_by_id),
                "fact_count": len(runtime.world.facts_by_id),
                "map_node_count": len(runtime.map.nodes_by_id),
                "map_edge_count": len([edge_id for edge_id in runtime.map.edges_by_id if "__reverse" not in edge_id]),
                "observer_count": len(runtime.cognition_by_observer),
                "narrative_node_count": len(runtime.narrative.nodes_by_id),
                "narrative_edge_count": len(runtime.narrative.edges_by_id),
                "active_world_line_ids": runtime.narrative.active_world_line_ids(),
                "open_gap_ids": runtime.narrative.open_gap_ids(),
            }

    def get_book_state_snapshot(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            snapshot = repo.latest_world_snapshot(project_id, as_of)
            if snapshot is None:
                runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of)
                return {
                    "project_id": project_id,
                    "as_of_chapter": as_of,
                    "snapshot": None,
                    "materialized": False,
                    "runtime": {
                        "world_node_count": len(runtime.world.nodes_by_id),
                        "world_edge_count": len(runtime.world.edges_by_id),
                        "fact_count": len(runtime.world.facts_by_id),
                        "map_node_count": len(runtime.map.nodes_by_id),
                        "map_edge_count": len([edge_id for edge_id in runtime.map.edges_by_id if "__reverse" not in edge_id]),
                    },
                }
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "materialized": True,
                "snapshot": snapshot.model_dump(mode="json"),
            }

    def list_book_state_nodes(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            nodes = repo.list_world_nodes(project_id, as_of_chapter=as_of)
            facts = repo.list_fact_nodes(project_id, as_of_chapter=as_of)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "nodes": [node.model_dump(mode="json") for node in nodes],
                "facts": [fact.model_dump(mode="json") for fact in facts],
            }

    def list_book_state_edges(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "edges": [
                    edge.model_dump(mode="json")
                    for edge in repo.list_world_edges(project_id, as_of_chapter=as_of)
                ],
            }

    def list_book_state_deltas(project_id: str, through_chapter: int = 0, after_chapter: int = -1) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            through = _resolve_as_of_chapter(session, project_id, through_chapter)
            repo = BookStateRepository(session)
            deltas = repo.list_graph_deltas(project_id, after_chapter=after_chapter, through_chapter=through)
            return {
                "project_id": project_id,
                "through_chapter": through,
                "after_chapter": after_chapter,
                "deltas": [delta.model_dump(mode="json") for delta in deltas],
            }

    def list_book_state_cognition(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "overlays": [
                    overlay.model_dump(mode="json")
                    for overlay in repo.list_cognition_overlays(project_id, as_of_chapter=as_of)
                ],
            }

    def list_book_state_reader_promises(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "reader_promises": [
                    promise.model_dump(mode="json")
                    for promise in repo.list_reader_promises_native(project_id, as_of_chapter=as_of)
                ],
                "reader_promise_nodes": [
                    node.model_dump(mode="json")
                    for node in repo.list_reader_promises(project_id, as_of_chapter=as_of)
                ],
                "reader_experience_deltas": [
                    item.model_dump(mode="json")
                    for item in repo.list_reader_experience_deltas(project_id, through_chapter=as_of)
                ],
            }

    def get_book_state_path(
        project_id: str,
        from_node_id: str,
        to_node_id: str,
        metric: str = "travel_time",
        as_of_chapter: int | None = None,
        observer_type: str = "",
        observer_id: str = "",
        allow_hidden: bool = False,
        allow_blocked: bool = False,
    ) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            observer = (observer_type, observer_id) if observer_type and observer_id else None
            runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of, observer_keys=[observer] if observer else None)
            result = runtime.map.shortest_path(from_node_id, to_node_id, metric=metric, observer=observer, allow_hidden=allow_hidden, allow_blocked=allow_blocked)
            return {
                "schema_version": "book_state.path.v1",
                "project_id": project_id,
                "as_of_chapter": as_of,
                **result.model_dump(mode="json"),
            }

    def list_personality_skills() -> dict[str, Any]:
        return _personality_library().catalog_payload()

    def create_character(project_id: str, req: CharacterCreateRequest) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            try:
                result = CharacterCreationHelper(session, personality_library=_personality_library()).create_character(
                    CharacterCreationRequest(project_id=project_id, **req.model_dump(mode="json"))
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            session.commit()
            return {
                "schema_version": "character.creation.v1",
                **result.model_dump(mode="json"),
            }

    def preview_character_personality(project_id: str, req: CharacterPersonalityPreviewRequest) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            policy = CharacterPersonalityPolicyResolver(session).resolve_for_project(project_id)
        result = PersonalityLoadoutAssigner(_personality_library()).preview(
            _assignment_request_from_preview(project_id, req, policy=policy)
        )
        return {
            "schema_version": "character.personality_preview.v1",
            "project_id": project_id,
            "personality_loadout": result.loadout.model_dump(mode="json", exclude_none=True),
            "personality_assignment": result.report.model_dump(mode="json"),
            "validation": result.validation.model_dump(mode="json"),
        }

    def preview_character_active_personality_context(
        project_id: str,
        req: CharacterPersonalityActiveContextPreviewRequest,
    ) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
        loadout = PersonalityLoadout.model_validate(req.personality_loadout or {})
        validation = PersonalityLoadoutAssigner(_personality_library()).validate(
            loadout.model_dump(mode="json", exclude_none=True)
        )
        if not validation.ok:
            raise HTTPException(status_code=400, detail=", ".join(validation.errors))
        context = build_active_personality_context(
            character_id=req.character_id,
            character_name=req.character_name,
            loadout=loadout,
            library=_personality_library(),
            scene_flags=req.scene_flags,
            pressure_triggers=req.pressure_triggers,
            relationship_targets=req.relationship_targets,
        )
        return {
            "schema_version": "character.active_personality_context_preview.v1",
            "project_id": project_id,
            "active_personality_context": context.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
        }

    def enrich_character_relationships(project_id: str, req: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = req or {}
        reason = str(payload.get("reason") or "manual relationship personality enrichment")
        with get_session() as session:
            _require_project(session, project_id)
            result = RelationshipPersonalityEnricher(
                session,
                personality_library=_personality_library(),
            ).enrich_project(project_id, reason=reason)
            session.commit()
            return {
                "schema_version": "character.relationship_personality_enrichment.v1",
                **result,
            }

    def get_character_personality_coverage(project_id: str, filter: str = "") -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            repo = BookStateRepository(session)
            nodes = [node for node in repo.list_world_nodes(project_id) if str(node.node_type) == "character"]
        assigner = PersonalityLoadoutAssigner(_personality_library())
        valid = 0
        missing = 0
        fallback = 0
        manual = 0
        needs_review = 0
        skill_distribution: dict[str, int] = {}
        characters: list[dict[str, Any]] = []
        issue_counts: dict[str, int] = {}
        for node in nodes:
            loadout = node.profile.get("personality_loadout") if isinstance(node.profile, dict) else None
            assignment = node.metadata.get("personality_assignment") if isinstance(node.metadata, dict) else {}
            item_issues: list[str] = []
            assignment_status = ""
            assignment_mode = ""
            manual_override = False
            if not loadout:
                missing += 1
                item_issues.append("missing_loadout")
            else:
                report = assigner.validate(loadout)
                item_issues.extend(report.errors)
                item_issues.extend(report.warnings)
                if report.ok:
                    valid += 1
                parsed_loadout = PersonalityLoadout.model_validate(loadout)
                if parsed_loadout.dominant is not None:
                    skill_distribution[parsed_loadout.dominant.skill] = skill_distribution.get(parsed_loadout.dominant.skill, 0) + 1
            if isinstance(assignment, dict):
                assignment_status = str(assignment.get("status") or "")
                assignment_mode = str(assignment.get("assignment_mode") or "")
                manual_override = bool(assignment.get("manual_override"))
                if manual_override:
                    manual += 1
                    item_issues.append("manual_override")
                if assignment_mode == "fallback_minimal" or assignment_status == "fallback_used":
                    fallback += 1
                    item_issues.append("fallback_used")
                if assignment_status == "valid_needs_review":
                    needs_review += 1
                    item_issues.append("valid_needs_review")
            for code in item_issues:
                issue_counts[code] = issue_counts.get(code, 0) + 1
            characters.append(
                {
                    "character_id": node.id,
                    "character_name": node.name,
                    "assignment_mode": assignment_mode,
                    "assignment_status": assignment_status,
                    "manual_override": manual_override,
                    "issues": item_issues,
                }
            )
        total = len(nodes)
        normalized_filter = str(filter or "").strip()
        if normalized_filter:
            characters = [
                item
                for item in characters
                if any(
                    issue == normalized_filter or str(issue).startswith(f"{normalized_filter}:")
                    for issue in item["issues"]
                )
            ]
        return {
            "schema_version": "character.personality_coverage.v1",
            "project_id": project_id,
            "character_count": total,
            "with_valid_loadout": valid,
            "missing_loadout": missing,
            "fallback_used": fallback,
            "manual_override": manual,
            "needs_review": needs_review,
            "coverage_ratio": 0.0 if total == 0 else round((total - missing) / total, 4),
            "issue_counts": issue_counts,
            "skill_distribution": skill_distribution,
            "personality_ooc_issue_counts": {},
            "characters": characters,
        }

    def get_character_personality_metrics(project_id: str) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            return build_character_personality_metrics(session, project_id)

    def backfill_character_personalities(project_id: str, req: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = req or {}
        dry_run = bool(payload.get("dry_run", False))
        respect_manual = bool(payload.get("respect_manual_override", True))
        reason = str(payload.get("reason") or "character personality backfill")
        items: list[dict[str, Any]] = []
        assigned = 0
        preserved = 0
        fallback_used = 0
        blocked = 0
        needs_review = 0
        with get_session() as session:
            _require_project(session, project_id)
            repo = BookStateRepository(session)
            assigner = PersonalityLoadoutAssigner(_personality_library())
            policy = CharacterPersonalityPolicyResolver(session).resolve_for_project(project_id)
            nodes = [node for node in repo.list_world_nodes(project_id) if str(node.node_type) == "character"]
            for node in nodes:
                existing = node.profile.get("personality_loadout") if isinstance(node.profile, dict) else None
                existing_assignment = node.metadata.get("personality_assignment") if isinstance(node.metadata, dict) else {}
                if existing and (not respect_manual or not (isinstance(existing_assignment, dict) and existing_assignment.get("manual_override"))):
                    preserved += 1
                    continue
                if existing:
                    preserved += 1
                    continue
                result = assigner.assign(
                    PersonalityAssignmentRequest(
                        project_id=project_id,
                        character_id=node.id,
                        character_name=node.name,
                        source="migration_backfill",
                        description=node.description,
                        summary=node.summary,
                        public_identity=str(node.profile.get("public_identity") or "") if isinstance(node.profile, dict) else "",
                        role_archetype=str(node.profile.get("role_archetype") or "") if isinstance(node.profile, dict) else "",
                        narrative_role=str(node.profile.get("narrative_role") or "") if isinstance(node.profile, dict) else "",
                        personality_tags=list(node.profile.get("personality_tags") or []) if isinstance(node.profile, dict) else [],
                        existing_cast_loadouts=_cast_loadouts(nodes, exclude_character_id=node.id),
                        policy=policy,
                    )
                )
                if not result.validation.ok:
                    blocked += 1
                    status = "blocked"
                else:
                    assigned += 1
                    status = "assigned"
                    if result.report.assignment_mode == "fallback_minimal":
                        fallback_used += 1
                    if result.report.status == "valid_needs_review":
                        needs_review += 1
                    if not dry_run:
                        profile = dict(node.profile)
                        metadata = dict(node.metadata)
                        profile["personality_loadout"] = result.loadout.model_dump(mode="json", exclude_none=True)
                        metadata["personality_assignment"] = result.report.model_dump(mode="json")
                        repo.create_world_node(node.model_copy(update={"profile": profile, "metadata": metadata}))
                items.append(
                    {
                        "character_id": node.id,
                        "status": status,
                        "selected_skill_ids": [item.skill for item in result.report.selected_skills],
                        "confidence": result.report.confidence,
                    }
                )
            if not dry_run:
                StateUpdater(session).save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        scope="character_creation",
                        event_family="business_event",
                        event_type=DecisionEventType.PERSONALITY_ASSIGNMENT_BACKFILL_COMPLETED,
                        actor_type="system",
                        summary="完成人物 personality_loadout backfill。",
                        reason=reason,
                        payload={"assigned": assigned, "preserved": preserved, "blocked": blocked},
                        related_object_type="project",
                        related_object_id=project_id,
                    )
                )
                session.commit()
        return {
            "schema_version": "character.personality_backfill.v1",
            "project_id": project_id,
            "scanned": assigned + preserved + blocked,
            "assigned": assigned,
            "preserved": preserved,
            "fallback_used": fallback_used,
            "blocked": blocked,
            "needs_review": needs_review,
            "dry_run": dry_run,
            "items": items,
        }

    def reassign_character_personality(
        project_id: str,
        character_id: str,
        req: CharacterPersonalityReassignRequest,
    ) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            repo = BookStateRepository(session)
            node = _get_character_node(repo, project_id, character_id, as_of_chapter=None)
            metadata = dict(node.metadata)
            existing_assignment = metadata.get("personality_assignment") if isinstance(metadata, dict) else {}
            if (
                req.respect_manual_override
                and not req.force
                and isinstance(existing_assignment, dict)
                and existing_assignment.get("manual_override")
            ):
                return {
                    "schema_version": "character.personality_reassign.v1",
                    "project_id": project_id,
                    "character_id": character_id,
                    "preserved": True,
                    "personality_assignment": existing_assignment,
                }
            if req.force and not str(req.reason or "").strip():
                raise HTTPException(status_code=400, detail="reason is required when force=true")
            old_loadout = dict(node.profile.get("personality_loadout") or {}) if isinstance(node.profile, dict) else {}
            policy = CharacterPersonalityPolicyResolver(session).resolve_for_project(project_id)
            nodes = [item for item in repo.list_world_nodes(project_id) if str(item.node_type) == "character"]
            result = PersonalityLoadoutAssigner(_personality_library()).assign(
                PersonalityAssignmentRequest(
                    project_id=project_id,
                    character_id=node.id,
                    character_name=node.name,
                    source="repair_reassign",
                    description=node.description,
                    summary=node.summary,
                    public_identity=str(node.profile.get("public_identity") or "") if isinstance(node.profile, dict) else "",
                    role_archetype=str(node.profile.get("role_archetype") or "") if isinstance(node.profile, dict) else "",
                    narrative_role=str(node.profile.get("narrative_role") or "") if isinstance(node.profile, dict) else "",
                    personality_tags=list(node.profile.get("personality_tags") or []) if isinstance(node.profile, dict) else [],
                    existing_cast_loadouts=_cast_loadouts(nodes, exclude_character_id=node.id),
                    policy=policy,
                )
            )
            profile = dict(node.profile)
            metadata = dict(node.metadata)
            new_loadout = result.loadout.model_dump(mode="json", exclude_none=True)
            diff = _loadout_diff(old_loadout, new_loadout, reason=req.reason)
            profile["personality_loadout"] = new_loadout
            metadata["personality_assignment"] = result.report.model_dump(mode="json")
            repo.create_world_node(node.model_copy(update={"profile": profile, "metadata": metadata}))
            StateUpdater(session).save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    scope="character_creation",
                    event_family="business_event",
                    event_type=DecisionEventType.PERSONALITY_LOADOUT_REASSIGNED,
                    actor_type="api",
                    summary=f"重新分配角色 {node.name or node.id} 的 personality_loadout。",
                    reason=req.reason,
                    payload={
                        "character_id": node.id,
                        "personality_assignment": result.report.model_dump(mode="json"),
                        "diff": diff,
                    },
                    related_object_type="world_node",
                    related_object_id=node.id,
                )
            )
            session.commit()
            return {
                "schema_version": "character.personality_reassign.v1",
                "project_id": project_id,
                "character_id": character_id,
                "preserved": False,
                "personality_loadout": new_loadout,
                "personality_assignment": result.report.model_dump(mode="json"),
                "diff": diff,
            }

    def get_character_assignment_report(project_id: str, character_id: str) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            node = _get_character_node(BookStateRepository(session), project_id, character_id, as_of_chapter=None)
            assignment = node.metadata.get("personality_assignment") if isinstance(node.metadata, dict) else {}
            return {
                "schema_version": "character.personality_assignment_report.v1",
                "project_id": project_id,
                "character_id": character_id,
                "personality_assignment": assignment or {},
            }

    def get_character_assignment_report_by_id(project_id: str, assignment_id: str) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            report = PersonalityAssignmentReportStore(session).explain(project_id, assignment_id)
            if report is None:
                raise HTTPException(status_code=404, detail="assignment report not found")
            return {
                "schema_version": "character.personality_assignment_report.v1",
                "project_id": project_id,
                **report,
            }

    def list_character_personality_loadouts(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            characters = [
                _character_personality_payload(node)
                for node in BookStateRepository(session).list_world_nodes(project_id, as_of_chapter=as_of)
                if str(node.node_type) == "character"
            ]
            return {
                "schema_version": "book_state.character_personality.v1",
                "project_id": project_id,
                "as_of_chapter": as_of,
                "characters": characters,
            }

    def get_character_personality_loadout(project_id: str, character_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            node = _get_character_node(
                BookStateRepository(session),
                project_id,
                character_id,
                as_of_chapter=as_of,
            )
            return {
                "schema_version": "book_state.character_personality.v1",
                "project_id": project_id,
                "as_of_chapter": as_of,
                **_character_personality_payload(node),
            }

    def set_character_personality_loadout(
        project_id: str,
        character_id: str,
        req: PersonalityLoadoutUpdateRequest,
    ) -> dict[str, Any]:
        loadout = PersonalityLoadout.model_validate(req.personality_loadout)
        loadout_payload = loadout.model_dump(mode="json", exclude_none=True)
        missing = _personality_library().validate_skill_ids(loadout.active_skill_ids())
        if missing:
            raise HTTPException(status_code=400, detail=f"unknown personality skills: {', '.join(missing)}")
        with get_session() as session:
            _require_project(session, project_id)
            repo = BookStateRepository(session)
            node = _get_character_node(repo, project_id, character_id, as_of_chapter=None)
            profile = dict(node.profile)
            profile["personality_loadout"] = loadout_payload
            metadata = dict(node.metadata)
            metadata["personality_assignment"] = {
                "assignment_id": f"manual_{str(character_id or '').strip()}",
                "policy_version": "character_personality_assignment.v1",
                "assignment_mode": "manual_world_studio",
                "confidence": 1.0,
                "status": "preserved_manual",
                "manual_override": True,
                "selected_skill_ids": sorted(loadout.active_skill_ids()),
                "reason": req.reason,
            }
            updated = node.model_copy(update={"profile": profile, "metadata": metadata})
            repo.create_world_node(updated)
            StateUpdater(session).save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    scope="book_state",
                    event_family="audit_action",
                    event_type=DecisionEventType.PERSONALITY_LOADOUT_MANUAL_OVERRIDE,
                    actor_type="api",
                    summary=f"更新角色 {updated.name or updated.id} 的 personality_loadout。",
                    reason=req.reason,
                    payload=audit_payload(
                        stage="personality_loadout",
                        status="updated",
                        project_id=project_id,
                        character_id=updated.id,
                        personality_loadout=loadout_payload,
                    ),
                    related_object_type="world_node",
                    related_object_id=updated.id,
                )
            )
            session.commit()
            return {
                "schema_version": "book_state.character_personality.v1",
                "project_id": project_id,
                **_character_personality_payload(updated),
            }

    return {
        "list_personality_skills": list_personality_skills,
        "create_character": create_character,
        "preview_character_personality": preview_character_personality,
        "preview_character_active_personality_context": preview_character_active_personality_context,
        "enrich_character_relationships": enrich_character_relationships,
        "get_character_personality_coverage": get_character_personality_coverage,
        "get_character_personality_metrics": get_character_personality_metrics,
        "backfill_character_personalities": backfill_character_personalities,
        "reassign_character_personality": reassign_character_personality,
        "get_character_assignment_report": get_character_assignment_report,
        "get_character_assignment_report_by_id": get_character_assignment_report_by_id,
        "get_book_state_runtime": get_book_state_runtime,
        "get_book_state_snapshot": get_book_state_snapshot,
        "list_book_state_nodes": list_book_state_nodes,
        "list_book_state_edges": list_book_state_edges,
        "list_book_state_deltas": list_book_state_deltas,
        "list_book_state_cognition": list_book_state_cognition,
        "list_book_state_reader_promises": list_book_state_reader_promises,
        "get_book_state_path": get_book_state_path,
        "list_character_personality_loadouts": list_character_personality_loadouts,
        "get_character_personality_loadout": get_character_personality_loadout,
        "set_character_personality_loadout": set_character_personality_loadout,
    }


def _get_character_node(
    repo: BookStateRepository,
    project_id: str,
    character_id: str,
    *,
    as_of_chapter: int | None,
):
    normalized = str(character_id or "").strip()
    for node in repo.list_world_nodes(project_id, as_of_chapter=as_of_chapter):
        if str(node.node_type) == "character" and node.id == normalized:
            return node
    raise HTTPException(status_code=404, detail="character not found")


def _character_personality_payload(node) -> dict[str, Any]:
    return {
        "character_id": node.id,
        "character_name": node.name,
        "personality_loadout": dict(node.profile.get("personality_loadout") or {}),
    }


def _loadout_diff(old_loadout: dict[str, Any], new_loadout: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    old_ids = _loadout_skill_ids(old_loadout)
    new_ids = _loadout_skill_ids(new_loadout)
    shared = old_ids.intersection(new_ids)
    changed = sorted(
        skill_id
        for skill_id in shared
        if _loadout_ref_by_skill(old_loadout, skill_id) != _loadout_ref_by_skill(new_loadout, skill_id)
    )
    return {
        "old_loadout": old_loadout,
        "new_loadout": new_loadout,
        "added_skill_ids": sorted(new_ids - old_ids),
        "removed_skill_ids": sorted(old_ids - new_ids),
        "changed_skill_ids": changed,
        "reason": reason,
    }


def _loadout_skill_ids(loadout: dict[str, Any]) -> set[str]:
    try:
        return PersonalityLoadout.model_validate(loadout or {}).active_skill_ids()
    except Exception:
        return set()


def _loadout_ref_by_skill(loadout: dict[str, Any], skill_id: str) -> dict[str, Any]:
    refs: list[dict[str, Any]] = []
    dominant = loadout.get("dominant") if isinstance(loadout, dict) else None
    if isinstance(dominant, dict):
        refs.append(dominant)
    for key in ("secondary", "social_mask", "stress_modes", "relationship_patterns"):
        values = loadout.get(key) if isinstance(loadout, dict) else None
        if isinstance(values, list):
            refs.extend(item for item in values if isinstance(item, dict))
    for ref in refs:
        if str(ref.get("skill") or "") == skill_id:
            return dict(ref)
    return {}


def _cast_loadouts(nodes: list[Any], *, exclude_character_id: str = "") -> list[dict[str, Any]]:
    loadouts: list[dict[str, Any]] = []
    for node in nodes:
        if exclude_character_id and getattr(node, "id", "") == exclude_character_id:
            continue
        profile = getattr(node, "profile", {}) if isinstance(getattr(node, "profile", {}), dict) else {}
        raw = profile.get("personality_loadout")
        if isinstance(raw, dict) and raw:
            loadouts.append(dict(raw))
    return loadouts


def _assignment_request_from_preview(
    project_id: str,
    req: CharacterPersonalityPreviewRequest,
    *,
    policy: CharacterPersonalityPolicy | None = None,
) -> PersonalityAssignmentRequest:
    profile = dict(req.profile)
    state = dict(req.state)
    return PersonalityAssignmentRequest(
        project_id=project_id,
        character_name=req.name,
        source=req.source,
        source_ref=req.source_ref,
        description=req.description,
        summary=req.summary,
        role_hint=str(profile.get("role_hint") or profile.get("role_archetype") or ""),
        narrative_role=str(profile.get("narrative_role") or ""),
        public_identity=str(profile.get("public_identity") or ""),
        role_archetype=str(profile.get("role_archetype") or ""),
        faction_id=str(state.get("faction_id") or ""),
        goal=str(state.get("goal") or ""),
        long_term_goal=str(state.get("long_term_goal") or ""),
        relationship_summary=str(state.get("relationship_summary") or ""),
        personality_tags=list(req.personality_tags or profile.get("personality_tags") or []),
        policy=policy or CharacterPersonalityPolicy(),
    )
