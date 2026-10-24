from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from forwin.book_state import BookStateRepository
from forwin.protocol.book_state import WorldNode
from .identity import CharacterIdentityMap


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
        name: str = "",
    ) -> CharacterResolutionResult:
        repo = BookStateRepository(self.session)
        normalized_id = str(character_id or "").strip()
        normalized_name = _normalize_name(name)
        identity = CharacterIdentityMap(self.session).resolve(
            project_id=project_id,
            character_id=normalized_id,
        )
        nodes = [node for node in repo.list_world_nodes(project_id) if str(node.node_type) == "character"]
        if identity is not None:
            target_id = identity.row.book_state_node_id or identity.row.canonical_character_id
            for node in nodes:
                if node.id == target_id:
                    return CharacterResolutionResult(node=node, resolution=identity.resolution)

        if normalized_id:
            for node in nodes:
                if node.id == normalized_id:
                    return CharacterResolutionResult(node=node, resolution="explicit_character_id")
        for node in nodes:
            if normalized_name and normalized_name in {_normalize_name(alias) for alias in node.aliases}:
                return CharacterResolutionResult(node=node, resolution="canonical_alias")
        for node in nodes:
            if normalized_name and _normalize_name(node.name) == normalized_name:
                return CharacterResolutionResult(node=node, resolution="exact_normalized_name")
        return CharacterResolutionResult(node=None, resolution="create_new")


def _normalize_name(value: str) -> str:
    return "".join(str(value or "").strip().lower().split())
