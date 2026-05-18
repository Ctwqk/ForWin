from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.fallbacks import *
from forwin.book_genesis_core.names_paths import *

def _normalize_world_payload(self, *, payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    normalized = payload if isinstance(payload, dict) else {}
    profiles_raw = [item for item in (normalized.get("culture_profiles") or fallback.get("culture_profiles") or []) if isinstance(item, dict)]
    culture_profiles: list[dict[str, Any]] = []
    profile_ids: set[str] = set()
    for index, item in enumerate(profiles_raw, start=1):
        profile_id = str(item.get("id", "")).strip() or f"culture-{index}"
        if profile_id in profile_ids:
            profile_id = f"{profile_id}-{index}"
        generator_civilization = _normalize_generator_civilization(item.get("generator_civilization", ""))
        if not generator_civilization:
            generator_civilization = _normalize_generator_civilization(
                item.get("inspiration", "") or item.get("name", "")
            )
        profile_payload = {
            "id": profile_id,
            "name": str(item.get("name", "")).strip() or f"文化背景{index}",
            "summary": str(item.get("summary", "")).strip(),
            "inspiration": str(item.get("inspiration", "")).strip(),
            "generator_civilization": generator_civilization,
            "generator_overlays": [
                _normalize_generator_civilization(value)
                for value in (item.get("generator_overlays") or [])
                if _normalize_generator_civilization(value)
            ],
            "social_markers": [str(value).strip() for value in (item.get("social_markers") or []) if str(value).strip()],
            "aesthetic_keywords": [str(value).strip() for value in (item.get("aesthetic_keywords") or []) if str(value).strip()],
            "character_name_style": str(item.get("character_name_style", "")).strip(),
            "region_name_style": str(item.get("region_name_style", "")).strip(),
            "location_name_style": str(item.get("location_name_style", "")).strip(),
            "character_name_examples": [str(value).strip() for value in (item.get("character_name_examples") or []) if str(value).strip()],
            "region_name_examples": [str(value).strip() for value in (item.get("region_name_examples") or []) if str(value).strip()],
            "location_name_examples": [str(value).strip() for value in (item.get("location_name_examples") or []) if str(value).strip()],
            "usage_notes": str(item.get("usage_notes", "")).strip(),
        }
        civilization = _culture_profile_generator_civilization(profile_payload)
        if civilization:
            try:
                if not profile_payload["character_name_examples"]:
                    profile_payload["character_name_examples"] = _generate_culture_names(
                        civilization=civilization,
                        kind="person",
                        count=4,
                        seed=f"{profile_id}:person",
                    )
                if not profile_payload["region_name_examples"]:
                    profile_payload["region_name_examples"] = _generate_culture_names(
                        civilization=civilization,
                        kind="region",
                        count=4,
                        seed=f"{profile_id}:region",
                    )
                if not profile_payload["location_name_examples"]:
                    profile_payload["location_name_examples"] = _generate_culture_names(
                        civilization=civilization,
                        kind="place",
                        count=4,
                        seed=f"{profile_id}:place",
                    )
            except Exception:  # noqa: BLE001
                logger.debug("Culture naming autofill failed for profile %s", profile_id, exc_info=True)
        culture_profiles.append(profile_payload)
        profile_ids.add(profile_id)
    return {
        "overview": str(normalized.get("overview", "")).strip() or str(fallback.get("overview", "")),
        "axioms": [str(item).strip() for item in (normalized.get("axioms") or fallback.get("axioms") or []) if str(item).strip()],
        "history_slice": str(normalized.get("history_slice", "")).strip() or str(fallback.get("history_slice", "")),
        "naming_style": str(normalized.get("naming_style", "")).strip() or str(fallback.get("naming_style", "")),
        "forbidden_zones": [str(item).strip() for item in (normalized.get("forbidden_zones") or fallback.get("forbidden_zones") or []) if str(item).strip()],
        "culture_profiles": culture_profiles,
    }

def _normalize_world_root_payload(
    self,
    *,
    project: Project,
    payload: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    normalized = payload if isinstance(payload, dict) else {}
    if not any(key in normalized for key in _WORLD_ROOT_KEYS) and any(
        key in normalized for key in _WORLD_BIBLE_KEYS
    ):
        normalized = {"world_bible": normalized}
    if isinstance(normalized.get("world"), dict):
        normalized = normalized.get("world") or {}
    result = _deep_merge(_empty_stage_world(), fallback if isinstance(fallback, dict) else {})
    result["minimum_world_system"] = _deep_merge(
        default_minimum_world_system(),
        normalized.get("minimum_world_system") if isinstance(normalized.get("minimum_world_system"), dict) else result.get("minimum_world_system", {}),
    )
    result["minimum_extension_pack"] = _deep_merge(
        default_minimum_extension_pack(),
        normalized.get("minimum_extension_pack") if isinstance(normalized.get("minimum_extension_pack"), dict) else result.get("minimum_extension_pack", {}),
    )
    result["world_bible"] = self._normalize_world_payload(
        payload=normalized.get("world_bible") if isinstance(normalized.get("world_bible"), dict) else {},
        fallback=result.get("world_bible") if isinstance(result.get("world_bible"), dict) else _fallback_world_bible(project, {"book_brief": result.get("book_brief")}),
    )
    result["map_atlas"] = self._normalize_map_payload(
        payload=normalized.get("map_atlas") if isinstance(normalized.get("map_atlas"), dict) else {},
        fallback=result.get("map_atlas") if isinstance(result.get("map_atlas"), dict) else {},
        world_bible=result["world_bible"],
    )
    result["story_engine"] = self._normalize_story_engine_payload(
        payload=normalized.get("story_engine") if isinstance(normalized.get("story_engine"), dict) else {},
        fallback=result.get("story_engine") if isinstance(result.get("story_engine"), dict) else {},
        world_bible=result["world_bible"],
        map_atlas=result["map_atlas"],
    )
    result["institution_profiles"] = [
        self._normalize_scope_profile(item, index=index, prefix="institution")
        for index, item in enumerate((normalized.get("institution_profiles") or result.get("institution_profiles") or []), start=1)
        if isinstance(item, dict)
    ]
    result["resource_economy_profiles"] = [
        self._normalize_scope_profile(item, index=index, prefix="economy")
        for index, item in enumerate((normalized.get("resource_economy_profiles") or result.get("resource_economy_profiles") or []), start=1)
        if isinstance(item, dict)
    ]
    world_extensions = normalized.get("world_extensions") if isinstance(normalized.get("world_extensions"), dict) else {}
    fallback_extensions = result.get("world_extensions") if isinstance(result.get("world_extensions"), dict) else default_world_extensions()
    merged_extensions = default_world_extensions()
    for key in merged_extensions:
        source_items = world_extensions.get(key) if isinstance(world_extensions.get(key), list) else fallback_extensions.get(key)
        merged_extensions[key] = [
            self._normalize_scope_profile(item, index=index, prefix=key[:-1] or "extension")
            for index, item in enumerate(source_items or [], start=1)
            if isinstance(item, dict)
        ]
    result["world_extensions"] = merged_extensions
    result["template_libraries"] = _deep_merge(
        default_template_libraries(),
        normalized.get("template_libraries") if isinstance(normalized.get("template_libraries"), dict) else result.get("template_libraries", {}),
    )
    return result

def _normalize_scope_profile(self, item: dict[str, Any], *, index: int, prefix: str) -> dict[str, Any]:
    payload = _json_clone(item)
    payload["id"] = str(payload.get("id", "")).strip() or f"{prefix}-{index}"
    payload["name"] = str(payload.get("name", "")).strip() or f"{prefix}-{index}"
    scope_ref = payload.get("scope_ref") if isinstance(payload.get("scope_ref"), dict) else {}
    applies_to = payload.get("applies_to") if isinstance(payload.get("applies_to"), dict) else {}
    payload["scope_ref"] = {
        "type": str(scope_ref.get("type", "") or applies_to.get("type", "")).strip(),
        "id": str(scope_ref.get("id", "") or applies_to.get("id", "")).strip(),
    }
    if applies_to:
        payload["applies_to"] = {
            "type": str(applies_to.get("type", "")).strip(),
            "id": str(applies_to.get("id", "")).strip(),
        }
    return payload

def _normalize_blueprint_payload(
    self,
    *,
    project: Project,
    payload: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    arcs = [item for item in (payload.get("arcs") or []) if isinstance(item, dict)]
    if not arcs:
        return fallback
    normalized: list[dict[str, Any]] = []
    chapter_cursor = 1
    story_focus = _story_focus_from_blueprint_fallback(project, fallback)
    for index, arc in enumerate(arcs, start=1):
        chapter_count = max(1, int(arc.get("chapter_count", 1) or 1))
        chapter_start = int(arc.get("chapter_start", chapter_cursor) or chapter_cursor)
        if chapter_start != chapter_cursor:
            chapter_start = chapter_cursor
        chapter_end = chapter_start + chapter_count - 1
        normalized.append(
            {
                "arc_number": index,
                "title": str(arc.get("title", "")).strip() or f"Arc {index}",
                "arc_synopsis": str(arc.get("arc_synopsis", "")).strip()
                or f"围绕“{story_focus}”推进第 {index} 段冲突。",
                "goal": str(arc.get("goal", "")).strip() or "推进主线并扩大代价",
                "stakes": str(arc.get("stakes", "")).strip() or "角色将承担更高损失风险",
                "payoff_direction": str(arc.get("payoff_direction", "")).strip() or "局部兑现 + 长线抬高",
                "chapter_start": chapter_start,
                "chapter_end": chapter_end,
                "chapter_count": chapter_count,
                "target_size": max(1, int(arc.get("target_size", chapter_count) or chapter_count)),
                "soft_min": max(1, int(arc.get("soft_min", max(1, int(round(chapter_count * 0.85)))) or 1)),
                "soft_max": max(
                    chapter_count,
                    int(arc.get("soft_max", max(chapter_count, int(round(chapter_count * 1.20)))) or chapter_count),
                ),
            }
        )
        chapter_cursor = chapter_end + 1
    return {
        "summary": str(payload.get("summary", "")).strip() or str(fallback.get("summary", "")),
        "arcs": normalized,
    }

def _normalize_map_payload(self, *, payload: dict[str, Any], fallback: dict[str, Any], world_bible: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = payload if isinstance(payload, dict) else {}
    culture_profile_ids = {
        str(item.get("id", "")).strip()
        for item in ((world_bible or {}).get("culture_profiles") or [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    submaps = [item for item in (normalized.get("submaps") or fallback.get("submaps") or []) if isinstance(item, dict)]
    normalized_submaps = []
    subworld_ids: set[str] = set()
    subworld_names: set[str] = set()
    subworld_name_to_id: dict[str, str] = {}
    for index, item in enumerate(submaps, start=1):
        subworld_id = str(item.get("id", "")).strip() or f"subworld-{index}"
        if subworld_id in subworld_ids:
            subworld_id = f"{subworld_id}-{index}"
        culture_profile_id = str(item.get("culture_profile_id", "")).strip()
        if culture_profile_id and culture_profile_id not in culture_profile_ids:
            culture_profile_id = ""
        name = str(item.get("name", "")).strip() or "未命名小世界"
        normalized_submaps.append(
            {
                "id": subworld_id,
                "name": name,
                "scope": str(item.get("scope", "")).strip() or "other",
                "parent_scope": str(item.get("parent_scope", "")).strip(),
                "culture_profile_id": culture_profile_id,
                "summary": str(item.get("summary", "")).strip(),
                "culture_traits": [str(value).strip() for value in (item.get("culture_traits") or []) if str(value).strip()],
                "climate": str(item.get("climate", "")).strip(),
                "terrain": [str(value).strip() for value in (item.get("terrain") or []) if str(value).strip()],
                "governing_power": str(item.get("governing_power", "")).strip(),
                "resident_factions": [str(value).strip() for value in (item.get("resident_factions") or []) if str(value).strip()],
                "key_locations": [str(value).strip() for value in (item.get("key_locations") or []) if str(value).strip()],
                "travel_rules": [str(value).strip() for value in (item.get("travel_rules") or []) if str(value).strip()],
                "resource_themes": [str(value).strip() for value in (item.get("resource_themes") or []) if str(value).strip()],
            }
        )
        subworld_ids.add(subworld_id)
        subworld_names.add(name)
        subworld_name_to_id[name] = subworld_id
    regions_raw = [item for item in (normalized.get("regions") or fallback.get("regions") or []) if isinstance(item, dict)]
    normalized_regions: list[dict[str, Any]] = []
    region_ids: set[str] = set()
    level_one_ids: dict[str, set[str]] = {}
    for index, item in enumerate(regions_raw, start=1):
        name = str(item.get("name", "")).strip() or f"地区{index}"
        subworld_name = str(item.get("subworld_name", "")).strip()
        if not subworld_name or subworld_name not in subworld_names:
            subworld_name = next(iter(subworld_names), "")
        level = int(item.get("level", 1) or 1)
        if level not in {1, 2}:
            level = 1
        region_id = str(item.get("id", "")).strip() or f"region-{index}"
        if region_id in region_ids:
            region_id = f"{region_id}-{index}"
        parent_region_id = str(item.get("parent_region_id", "")).strip()
        culture_profile_id = str(item.get("culture_profile_id", "")).strip()
        if culture_profile_id and culture_profile_id not in culture_profile_ids:
            culture_profile_id = ""
        if level == 1:
            parent_region_id = ""
            level_one_ids.setdefault(subworld_name, set()).add(region_id)
        else:
            valid_parents = level_one_ids.setdefault(subworld_name, set())
            if parent_region_id not in valid_parents:
                parent_region_id = next(iter(valid_parents), "")
                if not parent_region_id:
                    level = 1
            if level == 1:
                parent_region_id = ""
                level_one_ids.setdefault(subworld_name, set()).add(region_id)
        normalized_regions.append(
            {
                "id": region_id,
                "name": name,
                "subworld_name": subworld_name,
                "parent_region_id": parent_region_id,
                "level": level,
                "culture_profile_id": culture_profile_id,
                "kind": str(item.get("kind", "")).strip() or ("local_region" if level == 1 else "district"),
                "summary": str(item.get("summary", "")).strip(),
                "culture_traits": [str(value).strip() for value in (item.get("culture_traits") or []) if str(value).strip()],
                "climate": str(item.get("climate", "")).strip(),
                "terrain": [str(value).strip() for value in (item.get("terrain") or []) if str(value).strip()],
                "controller_factions": [str(value).strip() for value in (item.get("controller_factions") or []) if str(value).strip()],
                "resource_themes": [str(value).strip() for value in (item.get("resource_themes") or []) if str(value).strip()],
            }
        )
        region_ids.add(region_id)
    normalized_nodes = []
    node_ids: set[str] = set()
    for index, item in enumerate((normalized.get("nodes") or fallback.get("nodes") or []), start=1):
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id", "")).strip() or f"node-{index}"
        if node_id in node_ids:
            node_id = f"{node_id}-{index}"
        parent_subworld = str(item.get("parent_subworld", "")).strip()
        if parent_subworld in subworld_name_to_id:
            parent_subworld = subworld_name_to_id[parent_subworld]
        elif parent_subworld not in subworld_ids:
            parent_subworld = next(iter(subworld_ids), "")
        parent_region_id = str(item.get("parent_region_id", "")).strip()
        if parent_region_id and parent_region_id not in region_ids:
            parent_region_id = ""
        culture_profile_id = str(item.get("culture_profile_id", "")).strip()
        if culture_profile_id and culture_profile_id not in culture_profile_ids:
            culture_profile_id = ""
        normalized_nodes.append(
            {
                "id": node_id,
                "name": str(item.get("name", "")).strip() or "未命名地点",
                "kind": str(item.get("kind", "")).strip() or "other",
                "parent_subworld": parent_subworld,
                "parent_region_id": parent_region_id,
                "culture_profile_id": culture_profile_id,
                "description": str(item.get("description", "")).strip(),
                "control": str(item.get("control", "")).strip(),
                "danger": str(item.get("danger", "")).strip(),
                "climate_note": str(item.get("climate_note", "")).strip(),
                "terrain_note": str(item.get("terrain_note", "")).strip(),
                "culture_note": str(item.get("culture_note", "")).strip(),
                "resources": [str(value).strip() for value in (item.get("resources") or []) if str(value).strip()],
            }
        )
        node_ids.add(node_id)
    result = {
        "overview": str(normalized.get("overview", "")).strip() or str(fallback.get("overview", "")),
        "topology_rules": [str(item).strip() for item in (normalized.get("topology_rules") or fallback.get("topology_rules") or []) if str(item).strip()],
        "submaps": normalized_submaps,
        "regions": normalized_regions,
        "nodes": normalized_nodes,
        "edges": [item for item in (normalized.get("edges") or fallback.get("edges") or []) if isinstance(item, dict)],
    }
    return result

def _normalize_story_engine_payload(
    self,
    *,
    payload: dict[str, Any],
    fallback: dict[str, Any],
    world_bible: dict[str, Any] | None = None,
    map_atlas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = payload if isinstance(payload, dict) else {}
    culture_profile_ids = {
        str(item.get("id", "")).strip()
        for item in ((world_bible or {}).get("culture_profiles") or [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    submap_rows = [item for item in ((map_atlas or {}).get("submaps") or []) if isinstance(item, dict)]
    node_rows = [item for item in ((map_atlas or {}).get("nodes") or []) if isinstance(item, dict)]
    submap_id_set = {str(item.get("id", "")).strip() for item in submap_rows if str(item.get("id", "")).strip()}
    submap_name_to_id = {
        str(item.get("name", "")).strip(): str(item.get("id", "")).strip()
        for item in submap_rows
        if str(item.get("name", "")).strip() and str(item.get("id", "")).strip()
    }
    node_id_set = {str(item.get("id", "")).strip() for item in node_rows if str(item.get("id", "")).strip()}
    node_name_to_id = {
        str(item.get("name", "")).strip(): str(item.get("id", "")).strip()
        for item in node_rows
        if str(item.get("name", "")).strip() and str(item.get("id", "")).strip()
    }

    def _normalize_subworld_ref(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text in submap_id_set:
            return text
        return submap_name_to_id.get(text, text)

    def _normalize_node_ref(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text in node_id_set:
            return text
        return node_name_to_id.get(text, text)

    def _normalize_memberships(items: Any, fallback_faction: str = "") -> list[dict[str, Any]]:
        memberships = []
        for index, item in enumerate(items or [], start=1):
            if not isinstance(item, dict):
                continue
            faction_name = str(item.get("faction_name", "")).strip() or (fallback_faction if index == 1 else "")
            if not faction_name:
                continue
            memberships.append(
                {
                    "faction_name": faction_name,
                    "relation": str(item.get("relation", "")).strip() or "member",
                    "rank": str(item.get("rank", "")).strip(),
                    "is_primary": bool(item.get("is_primary")),
                }
            )
        if memberships and not any(item.get("is_primary") for item in memberships):
            memberships[0]["is_primary"] = True
        if memberships:
            primary_seen = False
            for item in memberships:
                if item.get("is_primary") and not primary_seen:
                    primary_seen = True
                else:
                    item["is_primary"] = False
        return memberships

    def _normalize_footprints(items: Any, fallback_subworld: str = "", fallback_region: str = "") -> list[dict[str, Any]]:
        footprints = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            subworld_name = _normalize_subworld_ref(item.get("subworld_name", "")) or fallback_subworld
            region_id = str(item.get("region_id", "")).strip() or fallback_region
            if not subworld_name and not region_id:
                continue
            footprints.append(
                {
                    "subworld_name": subworld_name,
                    "region_id": region_id,
                    "presence": str(item.get("presence", "")).strip() or "medium",
                    "mode": str(item.get("mode", "")).strip() or "rule",
                }
            )
        return footprints

    core_cast = []
    for item in (normalized.get("core_cast") or fallback.get("core_cast") or []):
        if not isinstance(item, dict):
            continue
        memberships = _normalize_memberships(item.get("faction_memberships"), fallback_faction=str(item.get("affiliated_faction", "")).strip())
        culture_profile_id = str(item.get("culture_profile_id", "")).strip()
        if culture_profile_id and culture_profile_id not in culture_profile_ids:
            culture_profile_id = ""
        core_cast.append(
            {
                "name": str(item.get("name", "")).strip() or "未命名角色",
                "role": str(item.get("role", "")).strip(),
                "desire": str(item.get("desire", "")).strip(),
                "fear": str(item.get("fear", "")).strip(),
                "secret": str(item.get("secret", "")).strip(),
                "culture_profile_id": culture_profile_id,
                "home_subworld": _normalize_subworld_ref(item.get("home_subworld", "")),
                "home_region": str(item.get("home_region", "")).strip(),
                "home_location": _normalize_node_ref(item.get("home_location", "")),
                "current_region": str(item.get("current_region", "")).strip(),
                "current_base": _normalize_node_ref(item.get("current_base", "")),
                "affiliated_faction": str(item.get("affiliated_faction", "")).strip() or (memberships[0]["faction_name"] if memberships else ""),
                "affiliated_family": str(item.get("affiliated_family", "")).strip(),
                "faction_memberships": memberships,
            }
        )
    factions = []
    faction_ids: set[str] = set()
    for item in (normalized.get("factions") or fallback.get("factions") or []):
        if not isinstance(item, dict):
            continue
        faction_id = str(item.get("id", "")).strip() or f"faction-{len(factions) + 1}"
        if faction_id in faction_ids:
            faction_id = f"{faction_id}-{len(factions) + 1}"
        footprints = _normalize_footprints(
            item.get("footprint"),
            fallback_subworld=_normalize_subworld_ref(item.get("base_subworld", "")),
            fallback_region=str(item.get("headquarters_region", "")).strip(),
        )
        culture_profile_id = str(item.get("culture_profile_id", "")).strip()
        if culture_profile_id and culture_profile_id not in culture_profile_ids:
            culture_profile_id = ""
        factions.append(
            {
                "id": faction_id,
                "name": str(item.get("name", "")).strip() or "未命名势力",
                "role": str(item.get("role", "")).strip(),
                "goal": str(item.get("goal", "")).strip(),
                "leverage": str(item.get("leverage", "")).strip(),
                "relationship_to_protagonist": str(item.get("relationship_to_protagonist", "")).strip(),
                "culture_profile_id": culture_profile_id,
                "base_subworld": _normalize_subworld_ref(item.get("base_subworld", "")),
                "headquarters_region": str(item.get("headquarters_region", "")).strip(),
                "base_location": _normalize_node_ref(item.get("base_location", "")),
                "territory_scope": [_normalize_subworld_ref(value) for value in (item.get("territory_scope") or []) if str(value).strip()],
                "culture_keywords": [str(value).strip() for value in (item.get("culture_keywords") or []) if str(value).strip()],
                "footprint": footprints,
            }
        )
        faction_ids.add(faction_id)
    faction_name_to_id = {
        str(item.get("name", "")).strip(): str(item.get("id", "")).strip()
        for item in factions
        if str(item.get("name", "")).strip() and str(item.get("id", "")).strip()
    }
    faction_id_set = {str(item.get("id", "")).strip() for item in factions if str(item.get("id", "")).strip()}

    def _normalize_faction_ref(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text in faction_id_set:
            return text
        return faction_name_to_id.get(text, text)

    for item in core_cast:
        item["affiliated_faction"] = _normalize_faction_ref(item.get("affiliated_faction", "")) or (
            _normalize_faction_ref(item["faction_memberships"][0]["faction_name"]) if item.get("faction_memberships") else ""
        )
    opposition = []
    for item in (normalized.get("opposition") or fallback.get("opposition") or []):
        if not isinstance(item, dict):
            continue
        backing_factions = [
            _normalize_faction_ref(value)
            for value in (item.get("backing_factions") or [])
            if str(value).strip()
        ]
        if not backing_factions and str(item.get("backing_faction", "")).strip():
            backing_factions = [_normalize_faction_ref(item.get("backing_faction", ""))]
        culture_profile_id = str(item.get("culture_profile_id", "")).strip()
        if culture_profile_id and culture_profile_id not in culture_profile_ids:
            culture_profile_id = ""
        opposition.append(
            {
                "name": str(item.get("name", "")).strip() or "未命名对手",
                "role": str(item.get("role", "")).strip(),
                "desire": str(item.get("desire", "")).strip(),
                "pressure": str(item.get("pressure", "")).strip(),
                "relationship_to_protagonist": str(item.get("relationship_to_protagonist", "")).strip(),
                "culture_profile_id": culture_profile_id,
                "base_subworld": _normalize_subworld_ref(item.get("base_subworld", "")),
                "base_region": str(item.get("base_region", "")).strip(),
                "base_location": _normalize_node_ref(item.get("base_location", "")),
                "backing_faction": _normalize_faction_ref(item.get("backing_faction", "")) or (backing_factions[0] if backing_factions else ""),
                "backing_factions": backing_factions,
            }
        )
    return {
        "core_cast": core_cast,
        "factions": factions,
        "opposition": opposition,
        "relationship_axes": [str(item).strip() for item in (normalized.get("relationship_axes") or fallback.get("relationship_axes") or []) if str(item).strip()],
        "reader_promises": [str(item).strip() for item in (normalized.get("reader_promises") or fallback.get("reader_promises") or []) if str(item).strip()],
        "long_arcs": [str(item).strip() for item in (normalized.get("long_arcs") or fallback.get("long_arcs") or []) if str(item).strip()],
    }



__all__ = ['_normalize_world_payload', '_normalize_world_root_payload', '_normalize_scope_profile', '_normalize_blueprint_payload', '_normalize_map_payload', '_normalize_story_engine_payload']
