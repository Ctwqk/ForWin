from __future__ import annotations

from typing import Any
from forwin.genesis.helpers import _book_arc_list, _map_node_table, _pack_stage_payload
from forwin.genesis.names_paths import (
    _name_hint_block,
)

def _refine_support_context(self, *, pack: dict[str, Any], stage_key: str) -> dict[str, Any]:
    if stage_key == "brief":
        return {}
    if stage_key == "world":
        return {"book_brief": pack.get("book_brief") or {}, "world": _pack_stage_payload(pack, "world")}
    if stage_key == "map":
        world_root = _pack_stage_payload(pack, "world")
        return {
            "book_brief": pack.get("book_brief") or {},
            "world_bible": world_root.get("world_bible") or {},
            "naming_assist": _name_hint_block(
                world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {},
                seed_prefix="refine:map",
            ),
        }
    if stage_key == "story_engine":
        world_root = _pack_stage_payload(pack, "world")
        return {
            "book_brief": pack.get("book_brief") or {},
            "world_bible": world_root.get("world_bible") or {},
            "map_atlas": world_root.get("map_atlas") or {},
            "naming_assist": _name_hint_block(
                world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {},
                seed_prefix="refine:story_engine",
            ),
        }
    if stage_key == "book_blueprint":
        world_root = _pack_stage_payload(pack, "world")
        return {
            "book_brief": pack.get("book_brief") or {},
            "world_bible": world_root.get("world_bible") or {},
            "map_atlas": world_root.get("map_atlas") or {},
            "story_engine": world_root.get("story_engine") or {},
        }
    if stage_key == "world_seed":
        return {
            "story_engine": _pack_stage_payload(pack, "story_engine"),
            "map_nodes": _map_node_table(_pack_stage_payload(pack, "map")),
            "arcs": _book_arc_list(pack),
        }
    return pack


__all__ = ['_refine_support_context']
