"""Deterministic Genesis fallbacks: structural shells only.

When the model cannot produce a stage, the fallback carries structure (ids, links, arc
sizes) and the author's own input, never invented story content. The normalizers fill
missing names with placeholders; the stage is marked ``degraded`` so the author knows to
generate or write it for real.
"""

from __future__ import annotations

from typing import Any

from forwin.arc_sizing import allocate_arc_chapter_sizes
from forwin.genesis.helpers import (
    _deep_merge,
    _empty_stage_world,
    _pack_stage_payload,
)
from forwin.models.project import Project


def _fallback_culture_profiles(project: Project, pack: dict[str, Any]) -> list[dict[str, Any]]:
    return []


def _fallback_world_bible(project: Project, pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "overview": project.setting_summary or "",
        "axioms": [],
        "history_slice": "",
        "naming_style": "",
        "forbidden_zones": [],
        "culture_profiles": _fallback_culture_profiles(project, pack),
    }


def _fallback_world(project: Project, pack: dict[str, Any]) -> dict[str, Any]:
    existing_world = _pack_stage_payload(pack, "world")
    fallback = _empty_stage_world()
    if existing_world:
        fallback = _deep_merge(fallback, existing_world)
    fallback["world_bible"] = _fallback_world_bible(project, pack)
    return fallback


def _fallback_map(pack: dict[str, Any]) -> dict[str, Any]:
    """One submap, one region and one node, linked by id and left unnamed."""
    return {
        "overview": "",
        "topology_rules": [],
        "submaps": [
            {
                "id": "subworld-1",
                "name": "",
                "scope": "continent",
                "parent_scope": "",
                "culture_profile_id": "",
                "summary": "",
                "culture_traits": [],
                "climate": "",
                "terrain": [],
                "governing_power": "",
                "resident_factions": [],
                "key_locations": [],
                "travel_rules": [],
                "resource_themes": [],
            }
        ],
        "regions": [
            {
                "id": "region-1",
                "name": "",
                "subworld_name": "",
                "parent_region_id": "",
                "level": 1,
                "culture_profile_id": "",
                "kind": "",
                "summary": "",
                "culture_traits": [],
                "climate": "",
                "terrain": [],
                "controller_factions": [],
                "resource_themes": [],
            }
        ],
        "nodes": [
            {
                "id": "node-1",
                "name": "",
                "kind": "",
                "parent_subworld": "subworld-1",
                "parent_region_id": "region-1",
                "culture_profile_id": "",
                "description": "",
                "control": "",
                "danger": "",
                "climate_note": "",
                "terrain_note": "",
                "culture_note": "",
                "resources": [],
            }
        ],
        "edges": [],
    }


def _fallback_story_engine(pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "core_cast": [],
        "factions": [],
        "opposition": [],
        "relationship_axes": [],
        "reader_promises": [],
        "long_arcs": [],
    }


def _fallback_blueprint(project: Project, pack: dict[str, Any]) -> dict[str, Any]:
    """Arc chapter ranges and sizes only; every narrative field stays empty."""
    total_chapters = max(
        1,
        int(
            (pack.get("book_brief") or {}).get("target_total_chapters")
            or project.target_total_chapters
            or 1
        ),
    )
    sizes = allocate_arc_chapter_sizes(total_chapters)
    arcs: list[dict[str, Any]] = []
    chapter_cursor = 1
    for index, chapter_count in enumerate(sizes, start=1):
        chapter_start = chapter_cursor
        chapter_end = chapter_cursor + chapter_count - 1
        arcs.append(
            {
                "arc_number": index,
                "title": "",
                "arc_synopsis": "",
                "goal": "",
                "stakes": "",
                "payoff_direction": "",
                "chapter_start": chapter_start,
                "chapter_end": chapter_end,
                "chapter_count": chapter_count,
                "target_size": chapter_count,
                "soft_min": max(1, int(round(chapter_count * 0.85))),
                "soft_max": max(chapter_count, int(round(chapter_count * 1.20))),
            }
        )
        chapter_cursor = chapter_end + 1
    return {"summary": "", "arcs": arcs}


__all__ = [name for name in globals() if not name.startswith("__")]
