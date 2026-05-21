from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state import BookStateRepository
from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.models.base import new_id
from forwin.personality import (
    CharacterPersonalityLibrary,
    PersonalityAssignmentRequest,
    PersonalityLoadoutAssigner,
)
from forwin.personality.policy import CharacterPersonalityPolicyResolver
from forwin.protocol.book_state import WorldNode
from forwin.review_engine.audit import build_legacy_compatibility_payload
from forwin.state.updater import StateUpdater

from .events import (
    CHARACTER_CREATED,
    CHARACTER_IMPORTED_FROM_LEGACY,
    CHARACTER_MERGED_EXISTING,
    CHARACTER_ROSTER_MATERIALIZED,
    PERSONALITY_LOADOUT_AUTO_ASSIGNED,
)
from .integrity import CharacterIntegrityIssue, CharacterIntegrityReport, failed_integrity
from .identity import CharacterIdentityMap
from .models import CharacterCreationRequest, CharacterCreationResult
from .normalization import is_generic_character_name
from .registry import CharacterRegistry


class CharacterCreationHelper:
    def __init__(
        self,
        session: Session,
        *,
        personality_library: CharacterPersonalityLibrary | None = None,
    ) -> None:
        self.session = session
        self.repo = BookStateRepository(session)
        self.registry = CharacterRegistry(session)
        self.library = personality_library or CharacterPersonalityLibrary()
        self.assigner = PersonalityLoadoutAssigner(self.library)
        self.policy_resolver = CharacterPersonalityPolicyResolver(session)

    def create_character(self, request: CharacterCreationRequest) -> CharacterCreationResult:
        if is_generic_character_name(request.name) and request.generic_character_policy == "reject_or_group":
            report = failed_integrity(
                "generic_character_rejected",
                message=f"{request.name} is a generic character token and should not become a named character.",
            )
            return CharacterCreationResult(
                project_id=request.project_id,
                character_name=request.name,
                integrity_report=report,
                warnings=[{"code": "generic_character_rejected", "message": report.errors[0].message}],
            )

        resolution = self.registry.resolve(
            project_id=request.project_id,
            character_id=request.character_id,
            legacy_entity_id=request.legacy_entity_id,
            roster_item_id=request.roster_item_id,
            name=request.name,
        )
        if resolution.node is not None and request.existing_resolution == "get_or_create":
            return self._existing_result(request, resolution.node)

        assignment = self.assigner.assign(self._assignment_request(request))
        loadout_payload = assignment.loadout.model_dump(mode="json", exclude_none=True)
        assignment_payload = assignment.report.model_dump(mode="json")
        if not assignment.validation.ok:
            raise ValueError(f"invalid personality_loadout: {', '.join(assignment.validation.errors)}")

        character_id = request.character_id.strip() if request.character_id.strip() else f"char_{new_id()}"
        legacy_entity_id = request.legacy_entity_id
        legacy_compat_event_id = ""
        if request.create_legacy_entity and not legacy_entity_id:
            entity = StateUpdater(self.session).create_entity(
                project_id=request.project_id,
                kind="character",
                name=request.name,
                description=request.description,
                aliases=request.aliases,
                importance=request.importance,
                chapter=request.created_at_chapter,
            )
            legacy_entity_id = entity.id
            legacy_compat_event_id = self._save_legacy_entity_compatibility_event(
                request,
                legacy_entity_id=legacy_entity_id,
                character_id=character_id,
            )

        genesis_ref_id = self._genesis_ref_id(request)
        profile = dict(request.profile)
        if request.personality_tags and not profile.get("personality_tags"):
            profile["personality_tags"] = list(request.personality_tags)
        profile["personality_loadout"] = loadout_payload
        metadata = {
            "legacy_entity_id": legacy_entity_id,
            "roster_item_ids": [request.roster_item_id] if request.roster_item_id else [],
            "character_identity": {
                "canonical_character_id": character_id,
                "book_state_node_id": character_id,
                "legacy_entity_id": legacy_entity_id,
                "genesis_ref_id": genesis_ref_id,
                "roster_item_ids": [request.roster_item_id] if request.roster_item_id else [],
            },
            "personality_assignment": assignment_payload,
            "character_creation": {
                "source": request.source,
                "source_ref": request.source_ref,
                "helper_version": "character_creation.v1",
                "dedupe_resolution": self._dedupe_resolution(request, resolution),
                "created_at_chapter": int(request.created_at_chapter or 0),
            },
        }
        if genesis_ref_id:
            metadata["genesis_ref_id"] = genesis_ref_id
        disambiguation = self._disambiguation_metadata(request, resolution)
        if disambiguation:
            metadata["character_creation"]["disambiguation"] = disambiguation
        node = WorldNode(
            id=character_id,
            project_id=request.project_id,
            node_type="character",
            name=request.name,
            aliases=list(request.aliases),
            summary=request.summary,
            description=request.description,
            importance=max(1, min(10, int(request.importance or 5))),
            created_at_chapter=int(request.created_at_chapter or 0),
            profile=profile,
            state=dict(request.state),
            metadata=metadata,
        )
        self.repo.create_world_node(node)
        self._sync_identity_map(
            request,
            character_id=node.id,
            legacy_entity_id=legacy_entity_id,
            display_name=node.name,
            aliases=list(node.aliases),
            genesis_ref_id=genesis_ref_id,
        )
        self.repo.append_world_node_state(
            project_id=request.project_id,
            node_id=node.id,
            node_type="character",
            as_of_chapter=int(request.created_at_chapter or 0),
            state=dict(request.state),
        )
        if legacy_entity_id and request.state:
            StateUpdater(self.session).create_entity_state(
                legacy_entity_id,
                int(request.created_at_chapter or 0),
                dict(request.state),
            )
        decision_ids = [
            self._save_event(
                request,
                self._creation_event_type(request),
                node.id,
                f"创建角色 {node.name or node.id}。",
                {"character_id": node.id, "character_name": node.name, "source": request.source},
            ),
            self._save_event(
                request,
                PERSONALITY_LOADOUT_AUTO_ASSIGNED,
                node.id,
                f"自动分配角色 {node.name or node.id} 的 personality_loadout。",
                {
                    "character_id": node.id,
                    "personality_assignment": {
                        "assignment_id": assignment.report.assignment_id,
                        "assignment_mode": assignment.report.assignment_mode,
                        "confidence": assignment.report.confidence,
                        "status": assignment.report.status,
                        "selected_skill_ids": [item.skill for item in assignment.report.selected_skills],
                        "reason_tags": list(assignment.report.reason_tags),
                    },
                },
            ),
        ]
        if legacy_compat_event_id:
            decision_ids.append(legacy_compat_event_id)
        return CharacterCreationResult(
            project_id=request.project_id,
            character_id=node.id,
            character_name=node.name,
            created=True,
            merged_existing=False,
            world_node=node.model_dump(mode="json"),
            legacy_entity_id=legacy_entity_id,
            roster_item_id=request.roster_item_id,
            personality_loadout=loadout_payload,
            personality_assignment=assignment.report,
            integrity_report=CharacterIntegrityReport(ok=assignment.validation.ok),
            decision_event_ids=decision_ids,
            warnings=[{"code": warning, "message": warning} for warning in assignment.validation.warnings],
        )

    def _creation_event_type(self, request: CharacterCreationRequest) -> str:
        if request.source == "legacy_entity_import":
            return CHARACTER_IMPORTED_FROM_LEGACY
        if request.source == "subworld_planned_slot_materialization":
            return CHARACTER_ROSTER_MATERIALIZED
        return CHARACTER_CREATED

    def _dedupe_resolution(
        self,
        request: CharacterCreationRequest,
        resolution,
    ) -> str:
        if request.existing_resolution == "create_new" and getattr(resolution, "node", None) is not None:
            return "created_new_disambiguated"
        return "created_new"

    def _disambiguation_metadata(
        self,
        request: CharacterCreationRequest,
        resolution,
    ) -> str:
        if request.existing_resolution != "create_new" or getattr(resolution, "node", None) is None:
            return ""
        context = dict(request.creation_context) if isinstance(request.creation_context, dict) else {}
        for key in ("disambiguation", "scope", "faction_id", "source_scope"):
            value = str(context.get(key) or "").strip()
            if value:
                return value
        return str(request.source_ref or request.roster_item_id or request.legacy_entity_id or "").strip()

    def get_or_create_character(self, request: CharacterCreationRequest) -> CharacterCreationResult:
        request = request.model_copy(update={"existing_resolution": "get_or_create"})
        return self.create_character(request)

    def materialize_roster_character(self, request: CharacterCreationRequest) -> CharacterCreationResult:
        return self.create_character(request.model_copy(update={"source": request.source or "subworld_planned_slot_materialization"}))

    def import_legacy_character(self, request: CharacterCreationRequest) -> CharacterCreationResult:
        return self.create_character(request.model_copy(update={"source": request.source or "legacy_entity_import"}))

    def apply_book_state_character_patch(self, request: CharacterCreationRequest) -> CharacterCreationResult:
        return self.create_character(request.model_copy(update={"source": request.source or "book_state_graph_delta"}))

    def ensure_character_integrity(self, character_id: str, *, reason: str) -> CharacterIntegrityReport:
        node = self.repo.get_world_node(character_id)
        if node is None:
            return failed_integrity("character_not_found", message=reason, character_id=character_id)
        if str(node.node_type) != "character":
            return failed_integrity(
                "not_character",
                message=f"{character_id} is not a character node.",
                character_id=character_id,
            )
        profile = dict(node.profile) if isinstance(node.profile, dict) else {}
        loadout = profile.get("personality_loadout")
        if not isinstance(loadout, dict) or not loadout:
            return failed_integrity(
                "personality_missing_loadout",
                message=reason,
                character_id=character_id,
            )
        validation = self.assigner.validate(loadout)
        errors = [
            CharacterIntegrityIssue(
                code=error,
                message=error,
                character_id=character_id,
            )
            for error in validation.errors
        ]
        warnings = [
            CharacterIntegrityIssue(
                code=warning,
                severity="warning",
                message=warning,
                character_id=character_id,
            )
            for warning in validation.warnings
        ]
        return CharacterIntegrityReport(
            ok=not errors,
            errors=errors,
            warnings=warnings,
            affected_character_ids=[character_id] if errors or warnings else [],
        )

    def _existing_result(self, request: CharacterCreationRequest, node: WorldNode) -> CharacterCreationResult:
        metadata = dict(node.metadata)
        self._sync_identity_map(
            request,
            character_id=node.id,
            legacy_entity_id=str(request.legacy_entity_id or metadata.get("legacy_entity_id") or ""),
            display_name=node.name,
            aliases=list(node.aliases),
            genesis_ref_id=self._genesis_ref_id(request) or str(metadata.get("genesis_ref_id") or ""),
        )
        self._save_event(
            request,
            CHARACTER_MERGED_EXISTING,
            node.id,
            f"复用已有角色 {node.name or node.id}。",
            {"character_id": node.id, "character_name": node.name, "resolution": "get_or_create"},
        )
        profile = dict(node.profile)
        return CharacterCreationResult(
            project_id=request.project_id,
            character_id=node.id,
            character_name=node.name,
            created=False,
            merged_existing=True,
            world_node=node.model_dump(mode="json"),
            legacy_entity_id=str(metadata.get("legacy_entity_id") or ""),
            personality_loadout=dict(profile.get("personality_loadout") or {}),
            personality_assignment=metadata.get("personality_assignment") or {},
            integrity_report=CharacterIntegrityReport(ok=bool(profile.get("personality_loadout"))),
        )

    def _sync_identity_map(
        self,
        request: CharacterCreationRequest,
        *,
        character_id: str,
        legacy_entity_id: str = "",
        display_name: str = "",
        aliases: list[str] | None = None,
        genesis_ref_id: str = "",
    ):
        return CharacterIdentityMap(self.session).upsert(
            project_id=request.project_id,
            canonical_character_id=character_id,
            book_state_node_id=character_id,
            legacy_entity_id=legacy_entity_id,
            genesis_ref_id=genesis_ref_id,
            roster_item_ids=[request.roster_item_id] if request.roster_item_id else [],
            aliases=list(aliases or request.aliases),
            display_name=display_name or request.name,
            metadata={
                "source": request.source,
                "source_ref": request.source_ref,
                "created_at_chapter": int(request.created_at_chapter or 0),
            },
        )

    def _genesis_ref_id(self, request: CharacterCreationRequest) -> str:
        context = dict(request.creation_context) if isinstance(request.creation_context, dict) else {}
        value = str(context.get("genesis_ref_id") or context.get("genesis_ref") or "").strip()
        if value:
            return value
        if str(request.source_ref or "").startswith("genesis:"):
            return str(request.source_ref or "").strip()
        if str(request.source or "").startswith("genesis") and request.source_ref:
            return str(request.source_ref or "").strip()
        return ""

    def _assignment_request(self, request: CharacterCreationRequest) -> PersonalityAssignmentRequest:
        profile = dict(request.profile)
        state = dict(request.state)
        existing_assignment: dict[str, Any] | None = None
        if request.personality_policy == "manual":
            existing_assignment = {"manual_override": True}
        return PersonalityAssignmentRequest(
            project_id=request.project_id,
            character_id=request.character_id,
            character_name=request.name,
            source=request.source,
            source_ref=request.source_ref,
            description=request.description,
            summary=request.summary,
            role_hint=str(profile.get("role_hint") or profile.get("role_archetype") or ""),
            narrative_role=str(profile.get("narrative_role") or ""),
            public_identity=str(profile.get("public_identity") or ""),
            role_archetype=str(profile.get("role_archetype") or ""),
            culture_tag=str(profile.get("culture_tag") or ""),
            origin_faction_id=str(profile.get("origin_faction_id") or ""),
            faction_id=str(state.get("faction_id") or ""),
            goal=str(state.get("goal") or ""),
            long_term_goal=str(state.get("long_term_goal") or ""),
            relationship_summary=str(state.get("relationship_summary") or ""),
            personality_tags=list(request.personality_tags or profile.get("personality_tags") or []),
            aliases=list(request.aliases),
            importance=int(request.importance or 5),
            explicit_loadout=request.personality_loadout,
            existing_assignment=existing_assignment,
            existing_cast_loadouts=self._existing_cast_loadouts(request.project_id),
            policy=self.policy_resolver.resolve_for_project(request.project_id),
        )

    def _existing_cast_loadouts(self, project_id: str) -> list[dict[str, Any]]:
        loadouts: list[dict[str, Any]] = []
        for node in self.repo.list_world_nodes(project_id):
            if str(node.node_type) != "character":
                continue
            raw = node.profile.get("personality_loadout") if isinstance(node.profile, dict) else None
            if isinstance(raw, dict) and raw:
                loadouts.append(dict(raw))
        return loadouts

    def _save_event(
        self,
        request: CharacterCreationRequest,
        event_type: str,
        character_id: str,
        summary: str,
        payload: dict[str, Any],
    ) -> str:
        row = StateUpdater(self.session).save_decision_event(
            DecisionEventInfo(
                project_id=request.project_id,
                scope="character_creation",
                event_family="business_event",
                event_type=event_type,
                actor_type="system",
                summary=summary,
                reason=request.audit_reason,
                payload=payload,
                related_object_type="world_node",
                related_object_id=character_id,
            )
        )
        return row.id

    def _save_legacy_entity_compatibility_event(
        self,
        request: CharacterCreationRequest,
        *,
        legacy_entity_id: str,
        character_id: str,
    ) -> str:
        row = StateUpdater(self.session).save_decision_event(
            DecisionEventInfo(
                project_id=request.project_id,
                chapter_number=int(request.created_at_chapter or 0),
                scope="character_creation",
                event_family="runtime_observation",
                event_type=DecisionEventType.LEGACY_COMPATIBILITY_USED,
                actor_type="system",
                summary="legacy compatibility used: characters.create_legacy_entity_default_true",
                reason="character creation materialized a legacy Entity row",
                payload=build_legacy_compatibility_payload(
                    compat_layer="characters",
                    compat_feature="characters.create_legacy_entity_default_true",
                    usage_kind="legacy_entity_create",
                    source_module="forwin.characters.creation",
                    usage_reason="character creation materialized a legacy Entity row",
                    compat_key="CharacterCreationRequest.create_legacy_entity",
                    legacy_identifier=legacy_entity_id,
                    canonical_identifier=character_id,
                    metadata={
                        "source": request.source,
                        "source_ref": request.source_ref,
                        "create_legacy_entity": bool(request.create_legacy_entity),
                    },
                ),
                related_object_type="entity",
                related_object_id=legacy_entity_id,
            )
        )
        return row.id
