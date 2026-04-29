from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from forwin.book_state import BookStateRepository
from forwin.protocol.book_state import WorldNode


@dataclass(frozen=True)
class CharacterResolutionResult:
    node: WorldNode | None
    resolution: str


class CharacterRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve(
        self,
        *,
        project_id: str,
        character_id: str = "",
        legacy_entity_id: str = "",
        roster_item_id: str = "",
        name: str = "",
    ) -> CharacterResolutionResult:
        repo = BookStateRepository(self.session)
        normalized_id = str(character_id or "").strip()
        normalized_legacy_id = str(legacy_entity_id or "").strip()
        normalized_roster_id = str(roster_item_id or "").strip()
        normalized_name = _normalize_name(name)
        nodes = [node for node in repo.list_world_nodes(project_id) if str(node.node_type) == "character"]

        if normalized_id:
            for node in nodes:
                if node.id == normalized_id:
                    return CharacterResolutionResult(node=node, resolution="explicit_character_id")
        if normalized_legacy_id:
            for node in nodes:
                metadata = dict(node.metadata) if isinstance(node.metadata, dict) else {}
                if str(metadata.get("legacy_entity_id") or "").strip() == normalized_legacy_id:
                    return CharacterResolutionResult(node=node, resolution="explicit_legacy_entity_id")
        if normalized_roster_id:
            for node in nodes:
                metadata = dict(node.metadata) if isinstance(node.metadata, dict) else {}
                roster_ids = metadata.get("roster_item_ids") if isinstance(metadata.get("roster_item_ids"), list) else []
                if normalized_roster_id in {str(item or "").strip() for item in roster_ids}:
                    return CharacterResolutionResult(node=node, resolution="explicit_roster_item_id")

        for node in nodes:
            if normalized_name and normalized_name in {_normalize_name(alias) for alias in node.aliases}:
                return CharacterResolutionResult(node=node, resolution="canonical_alias")
        for node in nodes:
            if normalized_name and _normalize_name(node.name) == normalized_name:
                return CharacterResolutionResult(node=node, resolution="exact_normalized_name")
        return CharacterResolutionResult(node=None, resolution="create_new")


def _normalize_name(value: str) -> str:
    return "".join(str(value or "").strip().lower().split())
