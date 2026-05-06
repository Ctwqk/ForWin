from __future__ import annotations

import json

from forwin.context.request import ContextDraft, ContextRequest


class GenesisContextProvider:
    name = "genesis"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        genesis_refs: dict[str, str] = {}
        genesis_world_overview = ""
        genesis_story_engine_summary = ""
        genesis_map_atlas: dict = {}
        genesis_story_engine: dict = {}
        genesis_getter = getattr(request.repo, "get_active_genesis_revision", None)
        if callable(genesis_getter):
            genesis_revision = genesis_getter(request.project_id)
            if genesis_revision is not None:
                try:
                    genesis_pack = json.loads(getattr(genesis_revision, "pack_json", "{}") or "{}") or {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    genesis_pack = {}
                if isinstance(genesis_pack, dict):
                    world_root = genesis_pack.get("world") if isinstance(genesis_pack.get("world"), dict) else {}
                    if not world_root:
                        world_root = {
                            "world_bible": genesis_pack.get("world_bible") if isinstance(genesis_pack.get("world_bible"), dict) else {},
                            "map_atlas": genesis_pack.get("map_atlas") if isinstance(genesis_pack.get("map_atlas"), dict) else {},
                            "story_engine": genesis_pack.get("story_engine") if isinstance(genesis_pack.get("story_engine"), dict) else {},
                        }
                    world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
                    genesis_map_atlas = world_root.get("map_atlas") if isinstance(world_root.get("map_atlas"), dict) else {}
                    story_engine = world_root.get("story_engine") if isinstance(world_root.get("story_engine"), dict) else {}
                    genesis_story_engine = story_engine
                    genesis_world_overview = str(world_bible.get("overview", "") or "")
                    long_arcs = story_engine.get("long_arcs") if isinstance(story_engine.get("long_arcs"), list) else []
                    genesis_story_engine_summary = "；".join(str(item).strip() for item in long_arcs if str(item).strip())
                    genesis_refs = {
                        "genesis_revision_id": str(getattr(genesis_revision, "id", "") or ""),
                        "genesis_revision_number": str(getattr(genesis_revision, "revision", "") or ""),
                    }
        draft.data.update(
            {
                "genesis_refs": genesis_refs,
                "genesis_world_overview": genesis_world_overview,
                "genesis_story_engine_summary": genesis_story_engine_summary,
                "genesis_map_atlas": genesis_map_atlas,
                "genesis_story_engine": genesis_story_engine,
            }
        )
