from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.arc_sizing import allocate_arc_chapter_sizes
from forwin.governance import DecisionEventInfo, DecisionEventType, normalize_project_governance
from forwin.model_adapter import ModelAdapter
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.naming import CULTURE_ALIAS_TO_KEY, CULTURES, CultureNameGenerator
from forwin.skills import (
    SkillPromptLayerBuilder,
    SkillRouter,
    inject_skill_layers,
    serialize_prompt_layers,
    summarize_selected_skills,
)
from forwin.state.updater import StateUpdater
from forwin.utils import LLMJSONParseError, parse_llm_json
from forwin.world_templates import (
    default_minimum_extension_pack,
    default_minimum_world_system,
    default_template_libraries,
    default_world_extensions,
    empty_world_root,
)
from forwin.writer.llm_client import LLMClient

logger = logging.getLogger(__name__)

GENESIS_STAGE_ORDER = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)
_STAGE_TO_SECTION = {
    "brief": "book_brief",
    "world": "world",
    "map": "world.map_atlas",
    "story_engine": "world.story_engine",
    "book_blueprint": "book_arc_blueprint",
    "bootstrap": "execution_bootstrap",
}
_PATH_TOKEN_RE = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")
_WORLD_ROOT_KEYS = {
    "minimum_world_system",
    "minimum_extension_pack",
    "world_bible",
    "map_atlas",
    "story_engine",
    "institution_profiles",
    "resource_economy_profiles",
    "world_extensions",
    "template_libraries",
}
_WORLD_BIBLE_KEYS = {
    "overview",
    "axioms",
    "history_slice",
    "naming_style",
    "forbidden_zones",
    "culture_profiles",
}
_WORLD_STAGE_RELATIVE_PREFIXES = {
    "minimum_world_system",
    "minimum_extension_pack",
    "world_bible",
    "map_atlas",
    "story_engine",
    "institution_profiles",
    "resource_economy_profiles",
    "world_extensions",
    "template_libraries",
}
_WORLD_STAGE_WORLD_BIBLE_ALIASES = {
    "overview",
    "axioms",
    "history_slice",
    "naming_style",
    "forbidden_zones",
    "culture_profiles",
}


class StaleGenesisRevisionError(RuntimeError):
    pass


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_clone(payload: Any) -> Any:
    return copy.deepcopy(payload)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _deep_equal(left: Any, right: Any) -> bool:
    return _json_dump(left) == _json_dump(right)


def _empty_stage_states() -> dict[str, dict[str, Any]]:
    return {
        stage_key: {
            "stage_key": stage_key,
            "status": "todo",
            "locked": False,
            "updated_at": "",
            "last_trace_id": "",
        }
        for stage_key in GENESIS_STAGE_ORDER
    }


def _ready_for_start(pack: dict[str, Any]) -> bool:
    stage_states = pack.get("stage_states") if isinstance(pack.get("stage_states"), dict) else {}
    return all(
        bool((stage_states.get(stage_key) or {}).get("locked"))
        for stage_key in GENESIS_STAGE_ORDER
    )


def _default_subworld_policy() -> dict[str, Any]:
    return {
        "root_first": True,
        "default_mode": "local_additive",
        "requires_parent_scope": True,
        "root_conflict_action": "block",
    }


def _empty_stage_world() -> dict[str, Any]:
    return {
        **empty_world_root(),
        "minimum_world_system": default_minimum_world_system(),
        "minimum_extension_pack": default_minimum_extension_pack(),
        "world_extensions": default_world_extensions(),
        "template_libraries": default_template_libraries(),
    }


def _legacy_world_root_from_pack(payload: dict[str, Any]) -> dict[str, Any]:
    world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
    base = _empty_stage_world()
    if world:
        base = _deep_merge(base, world)
    if isinstance(payload.get("world_bible"), dict):
        base["world_bible"] = _deep_merge(base.get("world_bible", {}), payload.get("world_bible") or {})
    if isinstance(payload.get("map_atlas"), dict):
        base["map_atlas"] = _deep_merge(base.get("map_atlas", {}), payload.get("map_atlas") or {})
    if isinstance(payload.get("story_engine"), dict):
        base["story_engine"] = _deep_merge(base.get("story_engine", {}), payload.get("story_engine") or {})
    return base


def _pack_stage_payload(pack: dict[str, Any], stage_key: str) -> dict[str, Any]:
    section_path = _STAGE_TO_SECTION[stage_key]
    if "." not in section_path:
        return pack.get(section_path) if isinstance(pack.get(section_path), dict) else {}
    current: Any = pack
    for token in section_path.split("."):
        if not isinstance(current, dict):
            return {}
        current = current.get(token)
    return current if isinstance(current, dict) else {}


def _set_pack_stage_payload(pack: dict[str, Any], stage_key: str, value: dict[str, Any]) -> None:
    section_path = _STAGE_TO_SECTION[stage_key]
    if "." not in section_path:
        pack[section_path] = value
        return
    current: Any = pack
    tokens = section_path.split(".")
    for token in tokens[:-1]:
        next_value = current.get(token)
        if not isinstance(next_value, dict):
            next_value = {}
            current[token] = next_value
        current = next_value
    current[tokens[-1]] = value


def _world_stage_target_path(path: str) -> str:
    normalized = str(path or "").strip()
    if not normalized:
        return normalized
    first_token_match = _PATH_TOKEN_RE.match(normalized)
    first_token = first_token_match.group(1) if first_token_match else ""
    if first_token in _WORLD_STAGE_RELATIVE_PREFIXES:
        return normalized
    if first_token in _WORLD_STAGE_WORLD_BIBLE_ALIASES:
        return f"world_bible.{normalized}"
    return normalized


def _normalize_stage_target_path(stage_key: str, target_path: str) -> str:
    if stage_key == "world":
        return _world_stage_target_path(target_path)
    return str(target_path or "").strip()


def _book_brief_from_project(project: Project, brief_seed: dict[str, Any] | None = None) -> dict[str, Any]:
    seed = brief_seed or {}
    return {
        "title": project.title,
        "premise": project.premise,
        "genre": project.genre,
        "target_total_chapters": int(project.target_total_chapters or 1),
        "setting_seed": project.setting_summary,
        "audience_hint": str(seed.get("audience_hint", "") or ""),
        "core_emotion": str(seed.get("core_emotion", "") or ""),
        "core_delight": str(seed.get("core_delight", "") or ""),
        "inspiration_notes": str(seed.get("inspiration_notes", "") or ""),
        "content_guardrails": [
            str(item).strip()
            for item in (seed.get("content_guardrails") or [])
            if str(item).strip()
        ],
        "narrative_promise": str(seed.get("narrative_promise", "") or ""),
    }


def _initial_pack(project: Project, brief_seed: dict[str, Any] | None = None) -> dict[str, Any]:
    governance = normalize_project_governance(project.governance_json)
    return {
        "book_brief": _book_brief_from_project(project, brief_seed),
        "world": _empty_stage_world(),
        "book_arc_blueprint": {},
        "subworld_policy": _default_subworld_policy(),
        "execution_bootstrap": {
            "operation_mode": "blackbox",
            "governance_defaults": governance.model_dump(mode="json"),
            "status": "draft",
        },
        "stage_states": _empty_stage_states(),
    }


def _fallback_brief(project: Project, book_brief: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": project.title,
        "one_line": f"{project.genre}长篇，围绕“{project.premise[:48]}”展开。",
        "audience": book_brief.get("audience_hint") or "网文读者",
        "core_emotion": book_brief.get("core_emotion") or "紧张与上升",
        "core_delight": book_brief.get("core_delight") or "危机升级、线索反转、主角成长",
        "promise": book_brief.get("narrative_promise") or "持续升级、逐步揭示世界真相。",
        "guardrails": book_brief.get("content_guardrails") or [],
    }


def _fallback_culture_profiles() -> list[dict[str, Any]]:
    return [
        {
            "id": "culture-main-stage",
            "name": "主舞台文化",
            "summary": "用于承接主舞台命名与文化语感的默认占位文化背景。",
            "inspiration": "待补充具体文化母本。",
            "generator_civilization": "中华",
            "generator_overlays": [],
            "social_markers": ["重秩序", "重门第", "旧俗与新制并行"],
            "aesthetic_keywords": ["冷色秩序", "旧城感", "压抑繁荣"],
            "character_name_style": "人物名以两到三字为主，简洁、冷硬、易记。",
            "region_name_style": "地区名强调功能或权力层级，如核心区、边境区、旧城带。",
            "location_name_style": "地点名强调辨识度与舞台感，如都城、渡口、要塞、旧街。",
            "character_name_examples": ["林烬", "沈砚"],
            "region_name_examples": ["主舞台核心区", "权力中心区"],
            "location_name_examples": ["主舞台", "权力中心", "危险边缘"],
            "usage_notes": "先保留结构，后续可替换为真实文化背景与命名映射。",
        }
    ]


def _fallback_world_bible(project: Project, pack: dict[str, Any]) -> dict[str, Any]:
    book_brief = pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {}
    return {
        "overview": project.setting_summary or f"{project.genre}世界，主角将在高压规则中逼近真相。",
        "axioms": [
            "力量、秩序和代价必须对应，强者不能无成本获得一切。",
            "世界规则应能支撑长期升级，而不是一次性解释完毕。",
        ],
        "history_slice": "当前时代处于旧秩序松动、新规则浮现的阶段。",
        "naming_style": "中文网文风格，专名简洁可记。",
        "forbidden_zones": book_brief.get("content_guardrails") or [],
        "culture_profiles": _fallback_culture_profiles(),
    }


def _fallback_world(project: Project, pack: dict[str, Any]) -> dict[str, Any]:
    existing_world = _pack_stage_payload(pack, "world")
    fallback = _empty_stage_world()
    if existing_world:
        fallback = _deep_merge(fallback, existing_world)
    fallback["world_bible"] = _fallback_world_bible(project, pack)
    return fallback


def _fallback_map(pack: dict[str, Any]) -> dict[str, Any]:
    world = _pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {}
    overview = str(world.get("overview", "") or "")
    culture_profiles = [item for item in (world.get("culture_profiles") or []) if isinstance(item, dict)]
    primary_profile = (culture_profiles[0] if culture_profiles else {}) or {}
    primary_culture_id = str(primary_profile.get("id", "") or "culture-main-stage") if culture_profiles else "culture-main-stage"
    primary_subworld_name = "主舞台总图"
    primary_region_name = "主舞台核心区"
    power_region_name = "权力中心区"
    primary_node_name = "主舞台"
    power_node_name = "权力中心"
    danger_node_name = "危险边缘"
    civilization = _culture_profile_generator_civilization(primary_profile)
    if civilization:
        try:
            primary_subworld_name = _generate_culture_names(
                civilization=civilization,
                kind="region",
                count=1,
                seed=f"{primary_culture_id}:subworld",
            )[0]
            primary_region_name = _generate_culture_names(
                civilization=civilization,
                kind="region",
                count=1,
                seed=f"{primary_culture_id}:region:main",
            )[0]
            power_region_name = _generate_culture_names(
                civilization=civilization,
                kind="region",
                count=1,
                seed=f"{primary_culture_id}:region:power",
            )[0]
            primary_node_name = _generate_culture_names(
                civilization=civilization,
                kind="place",
                count=1,
                seed=f"{primary_culture_id}:node:main",
            )[0]
            power_node_name = _generate_culture_names(
                civilization=civilization,
                kind="place",
                count=1,
                seed=f"{primary_culture_id}:node:power",
            )[0]
            danger_node_name = _generate_culture_names(
                civilization=civilization,
                kind="place",
                count=1,
                seed=f"{primary_culture_id}:node:danger",
            )[0]
        except Exception:  # noqa: BLE001
            logger.debug("Fallback map naming generation failed for profile %s", primary_culture_id, exc_info=True)
    primary_region_id = "region-main-stage"
    power_region_id = "region-power-core"
    primary_subworld_id = "subworld-main-stage"
    primary_node_id = "node-main-stage"
    power_node_id = "node-power-core"
    danger_node_id = "node-danger-edge"
    return {
        "overview": "结构化地图 V1",
        "topology_rules": [
            "地点之间必须有可解释的移动成本与阻隔。",
            "重要势力范围要和剧情活动区域一致。",
        ],
        "submaps": [
            {
                "id": primary_subworld_id,
                "name": primary_subworld_name,
                "scope": "macro_region",
                "parent_scope": "",
                "culture_profile_id": primary_culture_id,
                "summary": overview[:80] or "故事主要发生的区域。",
                "culture_traits": ["高压秩序", "旧俗仍在影响当代决策"],
                "climate": "四季分明但边缘区域气候异常。",
                "terrain": ["平原", "旧城", "边缘荒野"],
                "governing_power": "主舞台权力中枢",
                "resident_factions": ["主舞台权力中枢"],
                "key_locations": [primary_node_name, power_node_name, danger_node_name],
                "travel_rules": ["重要地点之间必须有可解释的移动成本。"],
                "resource_themes": ["关键资源", "旧时代遗留物"],
            }
        ],
        "regions": [
            {
                "id": primary_region_id,
                "name": primary_region_name,
                "subworld_name": primary_subworld_name,
                "parent_region_id": "",
                "level": 1,
                "culture_profile_id": primary_culture_id,
                "kind": "central_domain",
                "summary": overview[:80] or "故事主要发生的核心地区。",
                "culture_traits": ["高压秩序", "旧俗与新制度并存"],
                "climate": "四季分明但边缘区域气候异常。",
                "terrain": ["平原", "旧城带"],
                "controller_factions": ["主舞台权力中枢"],
                "resource_themes": ["人口", "制度资源", "情报"],
            },
            {
                "id": power_region_id,
                "name": power_region_name,
                "subworld_name": primary_subworld_name,
                "parent_region_id": primary_region_id,
                "level": 2,
                "culture_profile_id": primary_culture_id,
                "kind": "capital_core",
                "summary": "主舞台秩序与权力最集中的地区。",
                "culture_traits": ["等级森严", "血统叙事强"],
                "climate": "城市微气候稳定。",
                "terrain": ["高密度城建", "旧城区"],
                "controller_factions": ["主舞台权力中枢"],
                "resource_themes": ["金流", "权力网络"],
            },
        ],
        "nodes": [
            {
                "id": primary_node_id,
                "name": primary_node_name,
                "kind": "region",
                "parent_subworld": primary_subworld_id,
                "parent_region_id": primary_region_id,
                "culture_profile_id": primary_culture_id,
                "description": overview[:80] or "故事主要发生的区域。",
                "control": "主舞台权力中枢",
                "danger": "中等，表面秩序稳定但暗流强。",
                "climate_note": "气候稳定，边缘区域存在异常波动。",
                "terrain_note": "平原与旧城交错。",
                "culture_note": "旧俗与新秩序长期拉扯。",
                "resources": ["人口", "情报", "基础资源"],
            },
            {
                "id": power_node_id,
                "name": power_node_name,
                "kind": "city",
                "parent_subworld": primary_subworld_id,
                "parent_region_id": power_region_id,
                "culture_profile_id": primary_culture_id,
                "description": "冲突与秩序交汇的核心地点。",
                "control": "主舞台权力中枢",
                "danger": "表面低，政治风险高。",
                "climate_note": "城市微气候稳定。",
                "terrain_note": "高密度城建与旧城区并存。",
                "culture_note": "等级分明，重视秩序与血统。",
                "resources": ["制度资源", "金流", "权力网络"],
            },
            {
                "id": danger_node_id,
                "name": danger_node_name,
                "kind": "frontier",
                "parent_subworld": primary_subworld_id,
                "parent_region_id": primary_region_id,
                "culture_profile_id": primary_culture_id,
                "description": "推动升级和揭示秘密的高风险区域。",
                "control": "多方争夺",
                "danger": "高，规则不稳定。",
                "climate_note": "气候波动明显，夜间更危险。",
                "terrain_note": "荒野、裂谷与遗迹混杂。",
                "culture_note": "幸存者文化强，流言与禁忌并行。",
                "resources": ["遗迹线索", "稀有材料"],
            },
        ],
        "edges": [
            {"from": primary_node_name, "to": power_node_name, "relation": "常规往返"},
            {"from": primary_node_name, "to": danger_node_name, "relation": "高风险探索"},
        ],
    }


def _fallback_story_engine(pack: dict[str, Any]) -> dict[str, Any]:
    book_brief = pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {}
    world_root = _pack_stage_payload(pack, "world")
    map_atlas = world_root.get("map_atlas") if isinstance(world_root.get("map_atlas"), dict) else {}
    world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
    submaps = [item for item in (map_atlas.get("submaps") or []) if isinstance(item, dict)]
    regions = [item for item in (map_atlas.get("regions") or []) if isinstance(item, dict)]
    nodes = [item for item in (map_atlas.get("nodes") or []) if isinstance(item, dict)]
    culture_profiles = [item for item in (world_bible.get("culture_profiles") or []) if isinstance(item, dict)]
    primary_culture_id = str((culture_profiles[0] or {}).get("id", "") or "culture-main-stage") if culture_profiles else "culture-main-stage"
    primary_subworld = str((submaps[0] or {}).get("id", "") or (submaps[0] or {}).get("name", "") or "subworld-main-stage") if submaps else "subworld-main-stage"
    primary_region_id = str((regions[0] or {}).get("id", "") or "region-main-stage") if regions else "region-main-stage"
    power_region_id = str((regions[1] or {}).get("id", "") or primary_region_id or "region-power-core") if len(regions) > 1 else (primary_region_id or "region-power-core")
    primary_location = str((nodes[0] or {}).get("id", "") or (nodes[0] or {}).get("name", "") or "node-main-stage") if nodes else "node-main-stage"
    power_location = str((nodes[1] or {}).get("id", "") or (nodes[1] or {}).get("name", "") or primary_location or "node-power-core") if len(nodes) > 1 else (primary_location or "node-power-core")
    primary_profile = (culture_profiles[0] if culture_profiles else {}) or {}
    protagonist_name = "主角"
    primary_faction = "主舞台权力中枢"
    primary_faction_id = "faction-main-stage"
    opposition_name = "对手盘"
    civilization = _culture_profile_generator_civilization(primary_profile)
    if civilization:
        try:
            protagonist_name = _generate_culture_names(
                civilization=civilization,
                kind="person",
                count=1,
                seed=f"{primary_culture_id}:core_cast",
            )[0]
            primary_faction = _generate_culture_names(
                civilization=civilization,
                kind="epithet",
                count=1,
                seed=f"{primary_culture_id}:faction",
            )[0]
            opposition_name = _generate_culture_names(
                civilization=civilization,
                kind="person",
                count=1,
                seed=f"{primary_culture_id}:opposition",
            )[0]
        except Exception:  # noqa: BLE001
            logger.debug("Fallback story naming generation failed for profile %s", primary_culture_id, exc_info=True)
    return {
        "core_cast": [
            {
                "name": protagonist_name,
                "role": "主视角",
                "desire": "摆脱被动处境并掌握真相",
                "fear": "在升级过程中失去最重要的人或自我",
                "secret": "与旧时代规则存在未公开的深层关联。",
                "culture_profile_id": primary_culture_id,
                "home_subworld": primary_subworld,
                "home_region": primary_region_id,
                "home_location": primary_location,
                "current_region": primary_region_id,
                "current_base": primary_location,
                "affiliated_faction": primary_faction_id,
                "affiliated_family": "主角原生家庭",
                "faction_memberships": [
                    {
                        "faction_name": primary_faction,
                        "relation": "member",
                        "rank": "外围关联者",
                        "is_primary": True,
                    }
                ],
            }
        ],
        "factions": [
            {
                "id": primary_faction_id,
                "name": primary_faction,
                "role": "长期势力盘",
                "goal": "维护既有秩序并控制关键资源",
                "leverage": "组织、人脉、制度优势",
                "relationship_to_protagonist": "既想利用又防备主角",
                "culture_profile_id": primary_culture_id,
                "base_subworld": primary_subworld,
                "headquarters_region": power_region_id,
                "base_location": power_location,
                "territory_scope": [primary_subworld],
                "culture_keywords": ["秩序", "血统", "控制"],
                "footprint": [
                    {
                        "subworld_name": primary_subworld,
                        "region_id": primary_region_id,
                        "presence": "strong",
                        "mode": "rule",
                    }
                ],
            }
        ],
        "opposition": [
            {
                "name": opposition_name,
                "role": "长期压力源",
                "desire": "维持或攫取更高层级的控制权",
                "pressure": "通过规则、资源与秘密不断抬高主角代价。",
                "relationship_to_protagonist": "试图把主角纳入自己的规则体系。",
                "culture_profile_id": primary_culture_id,
                "base_subworld": primary_subworld,
                "base_region": power_region_id,
                "base_location": power_location,
                "backing_faction": primary_faction_id,
                "backing_factions": [primary_faction_id],
            }
        ],
        "relationship_axes": ["主角与对手盘的控制权争夺", "主角与盟友之间的信任成本"],
        "reader_promises": [
            book_brief.get("core_delight") or "升级、反转、揭秘",
            book_brief.get("core_emotion") or "持续紧张与兑现感",
        ],
        "long_arcs": ["主线谜团逐步展开", "主角成长与代价同步升级"],
    }


def _fallback_blueprint(project: Project, pack: dict[str, Any]) -> dict[str, Any]:
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
                "title": f"Arc {index}",
                "arc_synopsis": f"围绕“{project.premise[:36]}”推进第 {index} 段核心冲突，并拉高下一段压力。",
                "goal": "推进主线冲突并制造新的更高层承诺",
                "stakes": "失去主动权、暴露关键秘密或付出更高代价",
                "payoff_direction": "阶段性兑现+更大悬念开启",
                "chapter_start": chapter_start,
                "chapter_end": chapter_end,
                "chapter_count": chapter_count,
                "target_size": chapter_count,
                "soft_min": max(1, int(round(chapter_count * 0.85))),
                "soft_max": max(chapter_count, int(round(chapter_count * 1.20))),
            }
        )
        chapter_cursor = chapter_end + 1
    return {
        "summary": f"《{project.title}》全书多 Arc 骨架。",
        "arcs": arcs,
    }


def _fallback_bootstrap(project: Project, pack: dict[str, Any]) -> dict[str, Any]:
    governance = normalize_project_governance(project.governance_json)
    return {
        "operation_mode": "blackbox",
        "governance_defaults": governance.model_dump(mode="json"),
        "root_ready": bool(pack.get("book_arc_blueprint")),
        "start_policy": "explicit_start_writing_only",
    }


def _field_expects_list(field_path: str) -> bool:
    normalized = str(field_path or "").strip()
    return normalized.endswith("_examples") or normalized in {
        "character_name_examples",
        "region_name_examples",
        "location_name_examples",
    }


def _infer_name_kind(*, stage_key: str, target_path: str, field_path: str) -> str:
    normalized_field = str(field_path or "").strip()
    normalized_target = str(target_path or "").strip()
    if normalized_field == "character_name_examples":
        return "person"
    if normalized_field == "region_name_examples":
        return "region"
    if normalized_field == "location_name_examples":
        return "place"
    if normalized_field == "name":
        if stage_key == "map":
            if normalized_target.startswith("nodes["):
                return "place"
            return "region"
        if stage_key == "story_engine":
            if normalized_target.startswith("factions["):
                return "epithet"
            return "person"
    return ""


def _normalize_generator_civilization(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text in CULTURE_ALIAS_TO_KEY:
        return CULTURES[CULTURE_ALIAS_TO_KEY[text]].display
    for alias, key in CULTURE_ALIAS_TO_KEY.items():
        if alias and alias in text:
            return CULTURES[key].display
    return text


def _culture_profile_generator_civilization(profile: dict[str, Any]) -> str:
    base = _normalize_generator_civilization(profile.get("generator_civilization", ""))
    overlays = []
    for item in (profile.get("generator_overlays") or []):
        normalized = _normalize_generator_civilization(item)
        if normalized and normalized != base and normalized not in overlays:
            overlays.append(normalized)
    if not base:
        inspiration = str(profile.get("inspiration", "") or profile.get("name", "") or "")
        base = _normalize_generator_civilization(inspiration)
    if not base:
        return ""
    return "+".join([base, *overlays])


def _generate_culture_names(
    *,
    civilization: str,
    kind: str,
    count: int,
    seed: str,
) -> list[str]:
    generator = CultureNameGenerator(seed=seed)
    result = generator.generate(civilization, kind, count=max(1, int(count or 1)))
    if isinstance(result, str):
        return [result]
    return [str(item).strip() for item in result if str(item).strip()]


def _culture_profile_name_hints(
    profile: dict[str, Any],
    *,
    seed_prefix: str,
) -> dict[str, Any]:
    civilization = _culture_profile_generator_civilization(profile)
    if not civilization:
        return {}
    try:
        return {
            "culture_profile_id": str(profile.get("id", "")).strip(),
            "culture_profile_name": str(profile.get("name", "")).strip(),
            "generator_civilization": civilization,
            "character_name_examples": _generate_culture_names(
                civilization=civilization,
                kind="person",
                count=5,
                seed=f"{seed_prefix}:person",
            ),
            "region_name_examples": _generate_culture_names(
                civilization=civilization,
                kind="region",
                count=5,
                seed=f"{seed_prefix}:region",
            ),
            "location_name_examples": _generate_culture_names(
                civilization=civilization,
                kind="place",
                count=5,
                seed=f"{seed_prefix}:place",
            ),
            "epithet_examples": _generate_culture_names(
                civilization=civilization,
                kind="epithet",
                count=3,
                seed=f"{seed_prefix}:epithet",
            ),
        }
    except Exception:  # noqa: BLE001
        logger.debug("Culture naming hint generation failed for profile %s", profile.get("id", ""), exc_info=True)
        return {}


def _name_hint_block(world_bible: dict[str, Any], *, seed_prefix: str) -> list[dict[str, Any]]:
    hints = []
    for index, profile in enumerate((world_bible.get("culture_profiles") or []), start=1):
        if not isinstance(profile, dict):
            continue
        hint = _culture_profile_name_hints(profile, seed_prefix=f"{seed_prefix}:{index}")
        if hint:
            hints.append(hint)
    return hints


def _parse_path_tokens(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for chunk in str(path or "").strip().split("."):
        if not chunk:
            continue
        matches = list(_PATH_TOKEN_RE.finditer(chunk))
        if not matches:
            raise ValueError(f"非法路径：{path}")
        for match in matches:
            key, index = match.groups()
            if key is not None:
                tokens.append(key)
            elif index is not None:
                tokens.append(int(index))
    return tokens


def _get_value_at_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for token in _parse_path_tokens(path):
        if isinstance(token, str):
            if not isinstance(current, dict) or token not in current:
                raise ValueError(f"路径不存在：{path}")
            current = current[token]
        else:
            if not isinstance(current, list) or token < 0 or token >= len(current):
                raise ValueError(f"路径不存在：{path}")
            current = current[token]
    return current


def _set_value_at_path(payload: dict[str, Any], path: str, value: Any) -> None:
    tokens = _parse_path_tokens(path)
    if not tokens:
        raise ValueError("target_path 不能为空")
    current: Any = payload
    for token in tokens[:-1]:
        if isinstance(token, str):
            if not isinstance(current, dict) or token not in current:
                raise ValueError(f"路径不存在：{path}")
            current = current[token]
        else:
            if not isinstance(current, list) or token < 0 or token >= len(current):
                raise ValueError(f"路径不存在：{path}")
            current = current[token]
    final_token = tokens[-1]
    if isinstance(final_token, str):
        if not isinstance(current, dict):
            raise ValueError(f"路径不存在：{path}")
        current[final_token] = value
        return
    if not isinstance(current, list) or final_token < 0 or final_token >= len(current):
        raise ValueError(f"路径不存在：{path}")
    current[final_token] = value


def _ensure_revision_is_current(session: Session, project: Project, revision: BookGenesisRevision) -> None:
    session.refresh(project)
    current_revision_id = str(getattr(project, "active_genesis_revision_id", "") or "")
    expected_revision_id = str(getattr(revision, "id", "") or "")
    if current_revision_id and expected_revision_id and current_revision_id != expected_revision_id:
        raise StaleGenesisRevisionError("Genesis 已被新的操作更新，请刷新后重试。")


class BookGenesisService:
    def __init__(
        self,
        *,
        llm_client: ModelAdapter,
        max_tokens: int = 1600,
        skill_router: SkillRouter | None = None,
        skill_prompt_layer_builder: SkillPromptLayerBuilder | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.max_tokens = max_tokens
        self.skill_router = skill_router
        self.skill_prompt_layer_builder = skill_prompt_layer_builder

    def create_initial_revision(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        brief_seed: dict[str, Any] | None = None,
    ):
        pack = _initial_pack(project, brief_seed)
        row = updater.create_book_genesis_revision(
            project_id=project.id,
            revision=1,
            pack_json=_json_dump(pack),
            status="draft",
        )
        project.active_genesis_revision_id = row.id
        project.creation_status = "creating"
        session.add(project)
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="business_event",
                event_type=DecisionEventType.GENESIS_CREATED,
                actor_type="api",
                summary="Book Genesis 根层已初始化。",
                payload={"revision": 1},
                related_object_type="book_genesis_revision",
                related_object_id=row.id,
            )
        )
        session.flush()
        return row

    def active_revision(self, session: Session, project: Project) -> BookGenesisRevision | None:
        revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
        if not revision_id:
            return None
        return session.get(BookGenesisRevision, revision_id)

    def load_pack(self, revision) -> dict[str, Any]:
        return _initial_pack_dummy_merge(_json_load_object(getattr(revision, "pack_json", "{}")))

    def patch_pack(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision,
        patch: dict[str, Any],
        reason: str = "",
    ):
        _ensure_revision_is_current(session, project, revision)
        current = self.load_pack(revision)
        previous_stage_payloads = {
            stage_key: _json_clone(_pack_stage_payload(current, stage_key))
            for stage_key in GENESIS_STAGE_ORDER
        }
        next_pack = _deep_merge(current, patch)
        if "world" in patch and isinstance(next_pack.get("world"), dict):
            next_pack["world"] = self._normalize_world_root_payload(
                project=project,
                payload=next_pack.get("world") or {},
                fallback=_fallback_world(project, current),
            )
        if "book_arc_blueprint" in patch and isinstance(next_pack.get("book_arc_blueprint"), dict):
            next_pack["book_arc_blueprint"] = self._normalize_blueprint_payload(
                project=project,
                payload=next_pack.get("book_arc_blueprint") or {},
                fallback=_fallback_blueprint(project, current),
            )
        now = _utc_iso()
        stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else {}
        for stage_key, section_key in _STAGE_TO_SECTION.items():
            patched = section_key in patch
            if not patched and "world" in patch and stage_key in {"world", "map", "story_engine"}:
                patched = not _deep_equal(previous_stage_payloads.get(stage_key), _pack_stage_payload(next_pack, stage_key))
            if not patched:
                continue
            state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
            state.update(
                {
                    "stage_key": stage_key,
                    "status": "edited",
                    "locked": False,
                    "updated_at": now,
                }
            )
            stage_states[stage_key] = state
        next_pack["stage_states"] = stage_states
        new_row = updater.create_book_genesis_revision(
            project_id=project.id,
            revision=int(getattr(revision, "revision", 0) or 0) + 1,
            pack_json=_json_dump(next_pack),
            based_on_revision_id=str(getattr(revision, "id", "") or ""),
            status="draft",
        )
        project.active_genesis_revision_id = new_row.id
        project.creation_status = "creating"
        session.add(project)
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="audit_action",
                event_type=DecisionEventType.GENESIS_UPDATED,
                actor_type="manual_ui",
                summary="Book Genesis 已更新。",
                reason=str(reason or ""),
                payload={"patched_sections": sorted(patch.keys())},
                related_object_type="book_genesis_revision",
                related_object_id=new_row.id,
            )
        )
        session.flush()
        return new_row

    def generate_stage(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision,
        stage_key: str,
        event_type: str = DecisionEventType.GENESIS_STAGE_GENERATED,
    ):
        if stage_key not in GENESIS_STAGE_ORDER:
            raise ValueError(f"未知 Genesis stage: {stage_key}")
        pack = self.load_pack(revision)
        generated, trace_payload = self._generate_stage_payload(project=project, pack=pack, stage_key=stage_key)
        _ensure_revision_is_current(session, project, revision)
        next_pack = dict(pack)
        _set_pack_stage_payload(next_pack, stage_key, generated)
        stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else _empty_stage_states()
        stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
        parent_trace_id = str(stage_state.get("last_trace_id", "") or "")
        stage_state.update(
            {
                "stage_key": stage_key,
                "status": "generated",
                "locked": False,
                "updated_at": _utc_iso(),
            }
        )
        stage_states[stage_key] = stage_state
        next_pack["stage_states"] = stage_states
        decision = updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="business_event",
                event_type=event_type,
                actor_type="api",
                summary=f"Genesis 阶段 {stage_key} 已生成。",
                payload={"stage_key": stage_key},
                related_object_type="book_genesis_revision",
                related_object_id=str(getattr(revision, "id", "") or ""),
            )
        )
        trace = updater.save_prompt_trace(
            project_id=project.id,
            genesis_revision_id=str(getattr(revision, "id", "") or ""),
            decision_event_id=decision.id,
            parent_trace_id=parent_trace_id,
            trace_scope="genesis",
            stage_key=stage_key,
            template_id=f"genesis:{stage_key}",
            template_version="v1",
            effective_system_prompt=str(trace_payload.get("effective_system_prompt", "")),
            prompt_layers_json=_json_dump(trace_payload.get("prompt_layers", [])),
            input_snapshot_json=_json_dump(trace_payload.get("input_snapshot", {})),
            model_profile_json=_json_dump(trace_payload.get("model_profile", {})),
            attempts_json=_json_dump(trace_payload.get("attempts", [])),
            output_summary_json=_json_dump(trace_payload.get("output_summary", {})),
        )
        stage_state["last_trace_id"] = trace.id
        new_row = updater.create_book_genesis_revision(
            project_id=project.id,
            revision=int(getattr(revision, "revision", 0) or 0) + 1,
            pack_json=_json_dump(next_pack),
            based_on_revision_id=str(getattr(revision, "id", "") or ""),
            status="draft",
        )
        project.active_genesis_revision_id = new_row.id
        project.creation_status = "creating"
        session.add(project)
        session.flush()
        return new_row, trace

    def refine_stage(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision,
        stage_key: str,
        instruction: str,
        target_path: str = "",
        reason: str = "",
    ):
        normalized_instruction = str(instruction or "").strip()
        normalized_path = str(target_path or "").strip()
        if stage_key not in GENESIS_STAGE_ORDER:
            raise ValueError(f"未知 Genesis stage: {stage_key}")
        if not normalized_instruction:
            raise ValueError("refine instruction 不能为空")
        pack = self.load_pack(revision)
        refined_payload, trace_payload = self._refine_stage_payload(
            project=project,
            pack=pack,
            stage_key=stage_key,
            instruction=normalized_instruction,
            target_path=normalized_path,
        )
        _ensure_revision_is_current(session, project, revision)
        next_pack = dict(pack)
        _set_pack_stage_payload(next_pack, stage_key, refined_payload)
        stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else _empty_stage_states()
        stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
        parent_trace_id = str(stage_state.get("last_trace_id", "") or "")
        stage_state.update(
            {
                "stage_key": stage_key,
                "status": "edited",
                "locked": False,
                "updated_at": _utc_iso(),
            }
        )
        stage_states[stage_key] = stage_state
        next_pack["stage_states"] = stage_states
        decision = updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="audit_action",
                event_type=DecisionEventType.GENESIS_STAGE_REFINED,
                actor_type="manual_ui",
                summary=f"Genesis 阶段 {stage_key} 已按指令改写。",
                reason=str(reason or normalized_instruction),
                payload={"stage_key": stage_key, "instruction": normalized_instruction, "target_path": normalized_path},
                related_object_type="book_genesis_revision",
                related_object_id=str(getattr(revision, "id", "") or ""),
            )
        )
        trace = updater.save_prompt_trace(
            project_id=project.id,
            genesis_revision_id=str(getattr(revision, "id", "") or ""),
            decision_event_id=decision.id,
            parent_trace_id=parent_trace_id,
            trace_scope="genesis_refine",
            stage_key=stage_key,
            template_id=f"genesis_refine:{stage_key}",
            template_version="v1",
            effective_system_prompt=str(trace_payload.get("effective_system_prompt", "")),
            prompt_layers_json=_json_dump(trace_payload.get("prompt_layers", [])),
            input_snapshot_json=_json_dump(trace_payload.get("input_snapshot", {})),
            model_profile_json=_json_dump(trace_payload.get("model_profile", {})),
            attempts_json=_json_dump(trace_payload.get("attempts", [])),
            output_summary_json=_json_dump(trace_payload.get("output_summary", {})),
        )
        stage_state["last_trace_id"] = trace.id
        new_row = updater.create_book_genesis_revision(
            project_id=project.id,
            revision=int(getattr(revision, "revision", 0) or 0) + 1,
            pack_json=_json_dump(next_pack),
            based_on_revision_id=str(getattr(revision, "id", "") or ""),
            status="draft",
        )
        project.active_genesis_revision_id = new_row.id
        project.creation_status = "creating"
        session.add(project)
        session.flush()
        return new_row, trace

    def lock_stage(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision,
        stage_key: str,
    ):
        if stage_key not in GENESIS_STAGE_ORDER:
            raise ValueError(f"未知 Genesis stage: {stage_key}")
        _ensure_revision_is_current(session, project, revision)
        pack = self.load_pack(revision)
        stage_states = pack.get("stage_states") if isinstance(pack.get("stage_states"), dict) else _empty_stage_states()
        stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
        stage_state.update(
            {
                "stage_key": stage_key,
                "status": "locked",
                "locked": True,
                "updated_at": _utc_iso(),
            }
        )
        stage_states[stage_key] = stage_state
        pack["stage_states"] = stage_states
        new_row = updater.create_book_genesis_revision(
            project_id=project.id,
            revision=int(getattr(revision, "revision", 0) or 0) + 1,
            pack_json=_json_dump(pack),
            based_on_revision_id=str(getattr(revision, "id", "") or ""),
            status="draft",
        )
        project.active_genesis_revision_id = new_row.id
        if _ready_for_start(pack):
            project.creation_status = "genesis_ready"
        session.add(project)
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="audit_action",
                event_type=DecisionEventType.GENESIS_STAGE_LOCKED,
                actor_type="manual_ui",
                summary=f"Genesis 阶段 {stage_key} 已锁定。",
                payload={"stage_key": stage_key},
                related_object_type="book_genesis_revision",
                related_object_id=new_row.id,
            )
        )
        session.flush()
        return new_row

    def build_detail(self, *, session: Session, project: Project) -> dict[str, Any]:
        revision = self.active_revision(session, project)
        pack = self.load_pack(revision) if revision is not None else _initial_pack(project)
        prompt_traces = session.execute(
            select(PromptTrace)
            .where(PromptTrace.project_id == project.id)
            .order_by(PromptTrace.created_at.desc())
            .limit(50)
        ).scalars().all()
        return {
            "project_id": project.id,
            "creation_status": str(getattr(project, "creation_status", "") or "legacy"),
            "active_genesis_revision_id": str(getattr(project, "active_genesis_revision_id", "") or ""),
            "revision": int(getattr(revision, "revision", 1) or 1),
            "pack": pack,
            "prompt_traces": [
                {
                    "id": row.id,
                    "trace_scope": row.trace_scope,
                    "stage_key": row.stage_key,
                    "template_id": row.template_id,
                    "template_version": row.template_version,
                    "effective_system_prompt": row.effective_system_prompt,
                    "prompt_layers": _json_load_object(row.prompt_layers_json).get("items", [])
                    if False
                    else _json_load_list_dicts(row.prompt_layers_json),
                    "input_snapshot": _json_load_object(row.input_snapshot_json),
                    "model_profile": _json_load_object(row.model_profile_json),
                    "attempts": _json_load_list_dicts(row.attempts_json),
                    "output_summary": _json_load_object(row.output_summary_json),
                    "decision_event_id": row.decision_event_id,
                    "parent_trace_id": row.parent_trace_id,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
                for row in prompt_traces
            ],
            "can_start_writing": _ready_for_start(pack)
            and str(getattr(project, "creation_status", "") or "") == "genesis_ready",
        }

    def generate_name_suggestions(
        self,
        *,
        project: Project,
        revision: BookGenesisRevision,
        stage_key: str,
        target_path: str,
        field_path: str,
        kind: str = "",
        count: int = 1,
        nonce: str = "",
        stage_payload_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise ValueError("未知 Genesis stage。")
        pack = self.load_pack(revision)
        if isinstance(stage_payload_override, dict):
            pack = dict(pack)
            _set_pack_stage_payload(pack, normalized_stage, stage_payload_override)
        stage_payload = _pack_stage_payload(pack, normalized_stage)
        normalized_target = str(target_path or "").strip()
        normalized_field = str(field_path or "").strip()
        if not normalized_field:
            raise ValueError("field_path 不能为空。")
        resolved_kind = str(kind or "").strip() or _infer_name_kind(
            stage_key=normalized_stage,
            target_path=normalized_target,
            field_path=normalized_field,
        )
        if resolved_kind not in {"person", "region", "place", "epithet"}:
            raise ValueError("无法推断命名类型，请显式提供 kind。")
        culture_profile = self._resolve_name_generation_profile(
            stage_key=normalized_stage,
            pack=pack,
            stage_payload=stage_payload,
            target_path=normalized_target,
        )
        civilization = _culture_profile_generator_civilization(culture_profile)
        if not civilization:
            raise ValueError("当前对象没有可用的文化背景命名配置。")
        normalized_count = max(1, min(int(count or 1), 12))
        try:
            suggestions = _generate_culture_names(
                civilization=civilization,
                kind=resolved_kind,
                count=normalized_count,
                seed=":".join(
                    [
                        str(project.id or ""),
                        str(getattr(revision, "id", "") or ""),
                        normalized_stage,
                        normalized_target,
                        normalized_field,
                        resolved_kind,
                        str(culture_profile.get("id", "") or ""),
                        str(nonce or ""),
                    ]
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"名称生成失败：{exc}") from exc
        applied_value: Any = suggestions
        if not _field_expects_list(normalized_field) and normalized_count == 1:
            applied_value = suggestions[0]
        return {
            "ok": True,
            "stage_key": normalized_stage,
            "target_path": normalized_target,
            "field_path": normalized_field,
            "kind": resolved_kind,
            "suggestions": suggestions,
            "applied_value": applied_value,
            "culture_profile_id": str(culture_profile.get("id", "")).strip(),
            "culture_profile_name": str(culture_profile.get("name", "")).strip(),
            "generator_civilization": civilization,
            "message": "已根据文化背景生成名称建议。",
        }

    def _resolve_name_generation_profile(
        self,
        *,
        stage_key: str,
        pack: dict[str, Any],
        stage_payload: dict[str, Any],
        target_path: str,
    ) -> dict[str, Any]:
        normalized_target_path = _normalize_stage_target_path(stage_key, target_path)
        world_root = _pack_stage_payload(pack, "world")
        world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
        profiles = [
            item
            for item in (world_bible.get("culture_profiles") or [])
            if isinstance(item, dict)
        ]
        profile_by_id = {
            str(item.get("id", "")).strip(): item
            for item in profiles
            if str(item.get("id", "")).strip()
        }
        if stage_key == "world" and normalized_target_path.startswith("world_bible.culture_profiles["):
            target = _get_value_at_path(stage_payload, normalized_target_path)
            if isinstance(target, dict):
                return target
        target_value = None
        if normalized_target_path:
            try:
                target_value = _get_value_at_path(stage_payload, normalized_target_path)
            except ValueError:
                target_value = None
        if isinstance(target_value, dict):
            culture_profile_id = str(target_value.get("culture_profile_id", "")).strip()
            if culture_profile_id and culture_profile_id in profile_by_id:
                return profile_by_id[culture_profile_id]
        if profiles:
            return profiles[0]
        return _fallback_culture_profiles()[0]

    def materialize_book_arcs(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision,
    ) -> list[ArcPlanVersion]:
        pack = self.load_pack(revision)
        blueprint = pack.get("book_arc_blueprint") if isinstance(pack.get("book_arc_blueprint"), dict) else {}
        arc_items = [item for item in (blueprint.get("arcs") or []) if isinstance(item, dict)]
        if not arc_items:
            raise ValueError("Genesis blueprint 尚未生成 arcs。")
        existing_rows = session.execute(
            select(ArcPlanVersion)
            .where(ArcPlanVersion.project_id == project.id)
            .order_by(ArcPlanVersion.arc_number.asc(), ArcPlanVersion.created_at.asc())
        ).scalars().all()
        if existing_rows:
            return existing_rows
        created: list[ArcPlanVersion] = []
        for index, arc_payload in enumerate(arc_items, start=1):
            created.append(
                updater.create_arc_plan(
                    project_id=project.id,
                    arc_synopsis=str(arc_payload.get("arc_synopsis", "")).strip() or f"Arc {index}",
                    version=index,
                    status="active" if index == 1 else "planned",
                    arc_number=int(arc_payload.get("arc_number", index) or index),
                    chapter_start=int(arc_payload.get("chapter_start", 1) or 1),
                    chapter_end=int(arc_payload.get("chapter_end", 0) or 0),
                    planned_target_size=int(arc_payload.get("target_size", 0) or 0),
                    planned_soft_min=int(arc_payload.get("soft_min", 0) or 0),
                    planned_soft_max=int(arc_payload.get("soft_max", 0) or 0),
                )
            )
        session.flush()
        return created

    def materialize_arc_chapter_plans(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision,
        arc_number: int,
        decision_event_id: str = "",
    ) -> ArcPlanVersion:
        pack = self.load_pack(revision)
        blueprint = pack.get("book_arc_blueprint") if isinstance(pack.get("book_arc_blueprint"), dict) else {}
        arc_payload = next(
            (
                item
                for item in (blueprint.get("arcs") or [])
                if isinstance(item, dict) and int(item.get("arc_number", 0) or 0) == int(arc_number or 0)
            ),
            None,
        )
        if arc_payload is None:
            raise ValueError(f"Genesis blueprint 不存在 arc {arc_number}")
        arc_row = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project.id,
                ArcPlanVersion.arc_number == int(arc_number or 0),
            )
            .limit(1)
        ).scalar_one_or_none()
        if arc_row is None:
            raise ValueError(f"Arc {arc_number} skeleton 不存在")
        existing = session.execute(
            select(func.count(ChapterPlan.id)).where(ChapterPlan.arc_plan_id == arc_row.id)
        ).scalar_one()
        if int(existing or 0) > 0:
            return arc_row
        chapter_start = int(arc_payload.get("chapter_start", 1) or 1)
        chapter_end = int(arc_payload.get("chapter_end", chapter_start) or chapter_start)
        chapter_count = max(1, int(arc_payload.get("chapter_count", chapter_end - chapter_start + 1) or 1))
        planned, trace_payload = self._plan_arc_chapters(
            project=project,
            pack=pack,
            arc_payload=arc_payload,
            chapter_count=chapter_count,
        )
        if str(decision_event_id or "").strip():
            updater.save_prompt_trace(
                project_id=project.id,
                genesis_revision_id=str(getattr(revision, "id", "") or ""),
                decision_event_id=str(decision_event_id or "").strip(),
                trace_scope="start_writing",
                stage_key=f"launch_arc_{arc_row.arc_number}",
                template_id=f"launch_arc_plan:{arc_row.arc_number}",
                template_version="v1",
                effective_system_prompt=str(trace_payload.get("effective_system_prompt", "")),
                prompt_layers_json=_json_dump(trace_payload.get("prompt_layers", [])),
                input_snapshot_json=_json_dump(trace_payload.get("input_snapshot", {})),
                model_profile_json=_json_dump(trace_payload.get("model_profile", {})),
                attempts_json=_json_dump(trace_payload.get("attempts", [])),
                output_summary_json=_json_dump(trace_payload.get("output_summary", {})),
            )
        for index in range(chapter_count):
            number = chapter_start + index
            item = planned[index] if index < len(planned) else {}
            updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc_row.id,
                chapter_number=number,
                title=str(item.get("title", "")).strip() or f"第{number}章",
                one_line=str(item.get("one_line", "")).strip() or f"推进 arc {arc_number} 冲突。",
                goals=[
                    str(goal).strip()
                    for goal in (item.get("goals") or [])
                    if str(goal).strip()
                ][:3]
                or ["推进主线冲突", "兑现当前阶段承诺"],
            )
        session.flush()
        return arc_row

    def promote_next_arc_if_needed(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision,
    ) -> bool:
        next_arc = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project.id,
                ArcPlanVersion.status == "planned",
            )
            .order_by(ArcPlanVersion.arc_number.asc(), ArcPlanVersion.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if next_arc is None:
            return False
        active_rows = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project.id,
                ArcPlanVersion.status == "active",
            )
        ).scalars().all()
        for row in active_rows:
            row.status = "completed"
            session.add(row)
        next_arc.status = "active"
        session.add(next_arc)
        self.materialize_arc_chapter_plans(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            arc_number=next_arc.arc_number,
        )
        session.flush()
        return True

    def _generate_stage_payload(
        self,
        *,
        project: Project,
        pack: dict[str, Any],
        stage_key: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if stage_key == "brief":
            fallback = _fallback_brief(project, pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {})
            messages = [
                {
                    "role": "system",
                    "content": "你是网文项目总策划，只输出 JSON 对象，不要 markdown。",
                },
                {
                    "role": "user",
                    "content": (
                        "请根据新书 brief 生成整本书级别的 BookBrief，只返回 JSON，字段至少包含："
                        "title、one_line、audience、core_emotion、core_delight、promise、guardrails。\n\n"
                        f"输入：{_json_dump(pack.get('book_brief') or {})}"
                    ),
                },
            ]
        elif stage_key == "world":
            fallback = _fallback_world(project, pack)
            messages = [
                {"role": "system", "content": "你是中文长篇世界观编辑，只输出 JSON 对象。"},
                {
                    "role": "user",
                    "content": (
                        "请根据 BookBrief 生成统一世界观根对象 WorldRoot，只返回 JSON。\n"
                        "WorldRoot 至少包含：minimum_world_system、minimum_extension_pack、world_bible、map_atlas、story_engine、"
                        "institution_profiles、resource_economy_profiles、world_extensions、template_libraries。\n"
                        "其中 world_bible 至少包含：overview、axioms、history_slice、naming_style、forbidden_zones、culture_profiles。\n"
                        "culture_profiles 表示一组可复用的文化背景与命名体系，每项至少包含："
                        "id、name、summary、inspiration、generator_civilization、generator_overlays、social_markers、aesthetic_keywords、"
                        "character_name_style、region_name_style、location_name_style、"
                        "character_name_examples、region_name_examples、location_name_examples、usage_notes。\n"
                        "minimum_world_system 与 minimum_extension_pack 需要保留为最小实例骨架；"
                        "institution_profiles、resource_economy_profiles 与 world_extensions 默认可以为空；"
                        "template_libraries 作为模板库保留结构即可。\n\n"
                        f"BookBrief：{_json_dump(pack.get('book_brief') or {})}\n"
                        f"当前 WorldRoot：{_json_dump(_pack_stage_payload(pack, 'world'))}"
                    ),
                },
            ]
        elif stage_key == "map":
            fallback = _fallback_map(pack)
            world_root = _pack_stage_payload(pack, "world")
            world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
            messages = [
                {"role": "system", "content": "你是小说地图规划器，只输出 JSON 对象。"},
                {
                    "role": "user",
                    "content": (
                        "请基于 WorldBible 生成结构化 MapAtlas，只返回 JSON，字段至少包含：overview、topology_rules、submaps、nodes、edges。\n\n"
                        "MapAtlas 需要显式包含 regions，结构为 submaps、regions、nodes、edges 四层信息。"
                        "regions 表示小世界下辖地区，最多两级嵌套。"
                        "submaps 与 nodes 需要带稳定 id 字段；如果 WorldBible 里已经有 culture_profiles，请尽量为 submaps、regions、nodes 补 culture_profile_id。\n\n"
                        f"WorldBible：{_json_dump(world_bible)}\n\n"
                        f"命名辅助：{_json_dump(_name_hint_block(world_bible, seed_prefix=f'{project.id}:map'))}"
                    ),
                },
            ]
        elif stage_key == "story_engine":
            fallback = _fallback_story_engine(pack)
            world_root = _pack_stage_payload(pack, "world")
            world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
            messages = [
                {"role": "system", "content": "你是长篇网文叙事引擎规划器，只输出 JSON 对象。"},
                {
                    "role": "user",
                    "content": (
                        "请基于 BookBrief 和 WorldBible 生成 StoryEngine，只返回 JSON，字段至少包含：core_cast、factions、opposition、relationship_axes、reader_promises、long_arcs。\n\n"
                        "角色要支持 home_subworld、home_region、home_location、current_region、current_base、"
                        "faction_memberships；势力要支持 id、headquarters_region、footprint；对手盘要支持 base_region、backing_factions。"
                        "如果 WorldBible 里已经有 culture_profiles，请尽量为角色、势力、对手补 culture_profile_id。\n\n"
                        f"BookBrief：{_json_dump(pack.get('book_brief') or {})}\n"
                        f"WorldBible：{_json_dump(world_bible)}\n"
                        f"MapAtlas：{_json_dump(world_root.get('map_atlas') or {})}\n"
                        f"命名辅助：{_json_dump(_name_hint_block(world_bible, seed_prefix=f'{project.id}:story_engine'))}"
                    ),
                },
            ]
        elif stage_key == "book_blueprint":
            fallback = _fallback_blueprint(project, pack)
            messages = [
                {"role": "system", "content": "你是整本书多 Arc 蓝图策划，只输出 JSON 对象。"},
                {
                    "role": "user",
                    "content": (
                        "请生成整本书级别的 BookArcBlueprint，只返回 JSON。顶层至少包含 summary 和 arcs。"
                        "arcs 每项必须包含 arc_number、title、arc_synopsis、goal、stakes、payoff_direction、"
                        "chapter_start、chapter_end、chapter_count、target_size、soft_min、soft_max。\n\n"
                        f"BookBrief：{_json_dump(pack.get('book_brief') or {})}\n"
                        f"WorldBible：{_json_dump(_pack_stage_payload(pack, 'world').get('world_bible') or {})}\n"
                        f"StoryEngine：{_json_dump(_pack_stage_payload(pack, 'story_engine') or {})}\n"
                        f"建议 arc 尺寸：{_json_dump(_fallback_blueprint(project, pack).get('arcs') or [])}"
                    ),
                },
            ]
        elif stage_key == "bootstrap":
            fallback = _fallback_bootstrap(project, pack)
            messages = [
                {"role": "system", "content": "你是写作执行启动器，只输出 JSON 对象。"},
                {
                    "role": "user",
                    "content": (
                        "请根据当前 Genesis 根层生成 ExecutionBootstrap，只返回 JSON，字段至少包含："
                        "operation_mode、governance_defaults、root_ready、start_policy。\n\n"
                        f"BookGenesis：{_json_dump(pack)}"
                    ),
                },
            ]
        else:
            raise ValueError(f"未知 Genesis stage: {stage_key}")
        payload, trace = self._call_json_with_trace(messages=messages, fallback=fallback, stage_key=stage_key)
        if stage_key == "book_blueprint":
            payload = self._normalize_blueprint_payload(project=project, payload=payload, fallback=fallback)
        elif stage_key == "world":
            payload = self._normalize_world_root_payload(project=project, payload=payload, fallback=fallback)
        elif stage_key == "map":
            payload = self._normalize_map_payload(
                payload=payload,
                fallback=fallback,
                world_bible=_pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {},
            )
        elif stage_key == "story_engine":
            payload = self._normalize_story_engine_payload(
                payload=payload,
                fallback=fallback,
                world_bible=_pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {},
                map_atlas=_pack_stage_payload(pack, "world").get("map_atlas") if isinstance(_pack_stage_payload(pack, "world").get("map_atlas"), dict) else {},
            )
        return payload, trace

    def _refine_stage_payload(
        self,
        *,
        project: Project,
        pack: dict[str, Any],
        stage_key: str,
        instruction: str,
        target_path: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized_target_path = _normalize_stage_target_path(stage_key, target_path)
        current_payload = _pack_stage_payload(pack, stage_key)
        fallback_stage_payload = current_payload or (
            _fallback_map(pack) if stage_key == "map"
            else _fallback_story_engine(pack) if stage_key == "story_engine"
            else _fallback_world(project, pack) if stage_key == "world"
            else _fallback_brief(project, pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {})
            if stage_key == "brief"
            else _fallback_blueprint(project, pack) if stage_key == "book_blueprint"
            else _fallback_bootstrap(project, pack)
        )
        if normalized_target_path:
            source_payload = current_payload if current_payload else fallback_stage_payload
            current_target = _get_value_at_path(source_payload, normalized_target_path)
            if isinstance(current_target, dict):
                fallback = _json_clone(current_target)
                messages = [
                    {
                        "role": "system",
                        "content": "你是小说 Genesis 协作编辑。你会根据用户指令只改写指定 JSON 子对象，并返回更新后的 JSON 对象。不要输出 markdown。",
                    },
                    {
                        "role": "user",
                        "content": (
                            f"当前阶段：{stage_key}\n"
                            f"目标路径：{target_path}\n"
                            f"用户指令：{instruction}\n\n"
                            f"当前子对象：{_json_dump(current_target)}\n\n"
                            f"当前整段 JSON：{_json_dump(current_payload)}\n\n"
                            "请只返回这个目标子对象的新 JSON，并尽量保留未被要求修改的信息。"
                        ),
                    },
                ]
                payload, trace = self._call_json_with_trace(
                    messages=messages,
                    fallback=fallback,
                    stage_key=f"{stage_key}:refine_item",
                    max_tokens=1400,
                )
            else:
                wrapped_fallback = {"value": _json_clone(current_target)}
                messages = [
                    {
                        "role": "system",
                        "content": "你是小说 Genesis 协作编辑。你会根据用户指令只改写指定 JSON 值，并返回包含 value 字段的 JSON 对象。不要输出 markdown。",
                    },
                    {
                        "role": "user",
                        "content": (
                            f"当前阶段：{stage_key}\n"
                            f"目标路径：{target_path}\n"
                            f"用户指令：{instruction}\n\n"
                            f"当前目标值：{_json_dump(current_target)}\n\n"
                            f"当前整段 JSON：{_json_dump(current_payload)}\n\n"
                            "请只返回一个 JSON 对象，格式必须是 {\"value\": <更新后的 JSON 值>}。"
                            "只改这个目标值，尽量保留未被要求修改的信息。"
                        ),
                    },
                ]
                payload, trace = self._call_json_with_trace(
                    messages=messages,
                    fallback=wrapped_fallback,
                    stage_key=f"{stage_key}:refine_item",
                    max_tokens=1400,
                )
                payload = payload.get("value", wrapped_fallback["value"]) if isinstance(payload, dict) else wrapped_fallback["value"]
            next_payload = _json_clone(current_payload)
            _set_value_at_path(next_payload, normalized_target_path, payload)
        else:
            messages = [
                {
                    "role": "system",
                    "content": "你是小说 Genesis 协作编辑。你会根据用户指令改写当前阶段的 JSON，并返回完整的新 JSON 对象。不要输出 markdown。",
                },
                {
                    "role": "user",
                    "content": (
                        f"当前阶段：{stage_key}\n"
                        f"用户指令：{instruction}\n\n"
                        f"当前 JSON：{_json_dump(current_payload)}\n\n"
                        f"上游 Genesis 上下文：{_json_dump(self._refine_support_context(pack=pack, stage_key=stage_key))}\n\n"
                        "请返回这个阶段更新后的完整 JSON，并尽量保留未被要求修改的信息。"
                    ),
                },
            ]
            payload, trace = self._call_json_with_trace(
                messages=messages,
                fallback=fallback_stage_payload,
                stage_key=f"{stage_key}:refine",
                max_tokens=1800,
            )
            next_payload = payload

        if stage_key == "book_blueprint":
            next_payload = self._normalize_blueprint_payload(
                project=project,
                payload=next_payload if isinstance(next_payload, dict) else {},
                fallback=_fallback_blueprint(project, pack),
            )
        elif stage_key == "world":
            next_payload = self._normalize_world_root_payload(
                project=project,
                payload=next_payload if isinstance(next_payload, dict) else {},
                fallback=_fallback_world(project, pack),
            )
        elif stage_key == "map":
            next_payload = self._normalize_map_payload(
                payload=next_payload if isinstance(next_payload, dict) else {},
                fallback=_fallback_map(pack),
                world_bible=_pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {},
            )
        elif stage_key == "story_engine":
            next_payload = self._normalize_story_engine_payload(
                payload=next_payload if isinstance(next_payload, dict) else {},
                fallback=_fallback_story_engine(pack),
                world_bible=_pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {},
                map_atlas=_pack_stage_payload(pack, "world").get("map_atlas") if isinstance(_pack_stage_payload(pack, "world").get("map_atlas"), dict) else {},
            )
        elif not isinstance(next_payload, dict):
            next_payload = fallback_stage_payload

        trace["input_snapshot"] = {
            **(trace.get("input_snapshot") if isinstance(trace.get("input_snapshot"), dict) else {}),
            "instruction": instruction,
            "target_path": target_path,
            "current_stage_payload": current_payload,
        }
        trace["output_summary"] = {
            **(trace.get("output_summary") if isinstance(trace.get("output_summary"), dict) else {}),
            "instruction": instruction,
            "target_path": target_path,
            "normalized_target_path": normalized_target_path,
        }
        return next_payload, trace

    def _call_json_with_trace(
        self,
        *,
        messages: list[dict[str, str]],
        fallback: dict[str, Any],
        stage_key: str,
        temperature: float = 0.45,
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        skill_selections, skill_layers = self._resolve_skill_layers(stage_key=stage_key)
        effective_messages = inject_skill_layers(messages, skill_layers)
        prompt_layers = serialize_prompt_layers(messages, skill_layers)
        selected_skills = summarize_selected_skills(skill_selections)
        effective_prompt = "\n\n".join(
            str(item.get("content", "")).strip()
            for item in effective_messages
            if str(item.get("role", "")).strip() == "system"
        )
        attempts_payload: list[dict[str, Any]] = []
        max_tokens = min(self.max_tokens, int(max_tokens or self.max_tokens))
        if hasattr(self.llm_client, "api_key") and not str(getattr(self.llm_client, "api_key", "") or "").strip():
            attempts_payload.append({"status": "fallback", "reason": "missing_api_key"})
            return fallback, self._trace_payload(
                stage_key=stage_key,
                effective_system_prompt=effective_prompt,
                messages=messages,
                prompt_layers=prompt_layers,
                selected_skills=selected_skills,
                attempts=attempts_payload,
                output_summary={"mode": "fallback", "payload": fallback},
            )
        retry_plan = [
            {"temperature": temperature, "max_tokens": max_tokens},
            {"temperature": max(0.2, temperature - 0.15), "max_tokens": max(480, min(max_tokens, 900))},
        ]
        for attempt_no, attempt in enumerate(retry_plan, start=1):
            try:
                raw = self.llm_client.chat(
                    effective_messages,
                    temperature=attempt["temperature"],
                    max_tokens=attempt["max_tokens"],
                    response_format={"type": "json_object"},
                )
                payload = parse_llm_json(raw, error_prefix=f"Genesis {stage_key}")
                attempts_payload.append(
                    {
                        "attempt": attempt_no,
                        "status": "success",
                        "temperature": attempt["temperature"],
                        "max_tokens": attempt["max_tokens"],
                    }
                )
                return payload, self._trace_payload(
                    stage_key=stage_key,
                    effective_system_prompt=effective_prompt,
                    messages=messages,
                    prompt_layers=prompt_layers,
                    selected_skills=selected_skills,
                    attempts=attempts_payload,
                    output_summary={"mode": "success", "payload": payload},
                )
            except Exception as exc:  # noqa: BLE001
                attempts_payload.append(
                    {
                        "attempt": attempt_no,
                        "status": "failed",
                        "temperature": attempt["temperature"],
                        "max_tokens": attempt["max_tokens"],
                        "error": str(exc),
                    }
                )
                if isinstance(exc, LLMJSONParseError) and exc.empty_response:
                    break
        logger.warning("Genesis stage %s fell back to deterministic scaffold.", stage_key)
        attempts_payload.append({"status": "fallback", "reason": "deterministic_scaffold"})
        return fallback, self._trace_payload(
            stage_key=stage_key,
            effective_system_prompt=effective_prompt,
            messages=messages,
            prompt_layers=prompt_layers,
            selected_skills=selected_skills,
            attempts=attempts_payload,
            output_summary={"mode": "fallback", "payload": fallback},
        )

    def _resolve_skill_layers(self, *, stage_key: str):
        if self.skill_router is None or self.skill_prompt_layer_builder is None:
            return [], []
        normalized_stage_key = str(stage_key or "").strip()
        selection_stage_key = normalized_stage_key
        task_family = "generate_stage_payload"
        if normalized_stage_key.startswith("launch_arc_"):
            selection_stage_key = "book_blueprint"
            task_family = "launch_arc_plan"
        elif ":refine" in normalized_stage_key:
            selection_stage_key = normalized_stage_key.split(":", 1)[0]
            task_family = "refine_stage_payload"
        selections = self.skill_router.select(
            scope="genesis",
            stage_key=selection_stage_key,
            task_family=task_family,
        )
        return selections, self.skill_prompt_layer_builder.build(selections)

    def _trace_payload(
        self,
        *,
        stage_key: str,
        effective_system_prompt: str,
        messages: list[dict[str, str]],
        prompt_layers: list[dict[str, Any]] | None = None,
        selected_skills: list[dict[str, str]] | None = None,
        attempts: list[dict[str, Any]],
        output_summary: dict[str, Any],
    ) -> dict[str, Any]:
        selected = list(selected_skills or [])
        return {
            "effective_system_prompt": effective_system_prompt,
            "prompt_layers": prompt_layers
            if prompt_layers is not None
            else [
                {"role": str(item.get("role", "")).strip(), "content": str(item.get("content", ""))}
                for item in messages
            ],
            "input_snapshot": {
                "stage_key": stage_key,
                "messages": messages,
                "selected_skills": selected,
            },
            "model_profile": {
                "profile_id": getattr(self.llm_client, "profile_id", ""),
                "profile_name": getattr(self.llm_client, "profile_name", ""),
                "model": getattr(self.llm_client, "model", ""),
                "base_url": getattr(self.llm_client, "base_url", ""),
            },
            "attempts": attempts,
            "output_summary": {
                **output_summary,
                "skill_summary": selected,
            },
        }

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
                    or f"围绕“{project.premise[:36]}”推进第 {index} 段冲突。",
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
        return pack

    def _plan_arc_chapters(
        self,
        *,
        project: Project,
        pack: dict[str, Any],
        arc_payload: dict[str, Any],
        chapter_count: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        fallback = [
            {
                "title": f"第{index}章",
                "one_line": f"围绕“{arc_payload.get('arc_synopsis', project.premise)[:28]}”推进冲突。",
                "goals": ["推进当前 arc 主线", "制造新线索或新代价"],
            }
            for index in range(1, chapter_count + 1)
        ]
        messages = [
            {"role": "system", "content": "你是 Arc 细化编辑，只输出 JSON 对象。"},
            {
                "role": "user",
                "content": (
                    f"请为当前 arc 规划恰好 {chapter_count} 章，只返回 JSON，顶层格式为 "
                    "{\"chapters\": [...]}，每项包含 title、one_line、goals。\n\n"
                    f"BookBrief：{_json_dump(pack.get('book_brief') or {})}\n"
                    f"WorldBible：{_json_dump(_pack_stage_payload(pack, 'world').get('world_bible') or {})}\n"
                    f"StoryEngine：{_json_dump(_pack_stage_payload(pack, 'story_engine') or {})}\n"
                    f"当前 Arc：{_json_dump(arc_payload)}"
                ),
            },
        ]
        payload, trace_payload = self._call_json_with_trace(
            messages=messages,
            fallback={"chapters": fallback},
            stage_key=f"launch_arc_{int(arc_payload.get('arc_number', 1) or 1)}",
            max_tokens=1200,
        )
        chapters = payload.get("chapters") if isinstance(payload, dict) else []
        normalized: list[dict[str, Any]] = []
        for index in range(1, chapter_count + 1):
            source = chapters[index - 1] if index - 1 < len(chapters) and isinstance(chapters[index - 1], dict) else {}
            goals = [
                str(item).strip()
                for item in (source.get("goals") or [])
                if str(item).strip()
            ][:3]
            normalized.append(
                {
                    "title": str(source.get("title", "")).strip() or fallback[index - 1]["title"],
                    "one_line": str(source.get("one_line", "")).strip() or fallback[index - 1]["one_line"],
                    "goals": goals or fallback[index - 1]["goals"],
                }
            )
        return normalized, trace_payload


def _initial_pack_dummy_merge(payload: dict[str, Any]) -> dict[str, Any]:
    base = {
        "book_brief": {},
        "world": _empty_stage_world(),
        "book_arc_blueprint": {},
        "subworld_policy": _default_subworld_policy(),
        "execution_bootstrap": {},
        "stage_states": _empty_stage_states(),
    }
    upgraded_payload = dict(payload or {})
    upgraded_payload["world"] = _legacy_world_root_from_pack(upgraded_payload)
    upgraded_payload.pop("world_bible", None)
    upgraded_payload.pop("map_atlas", None)
    upgraded_payload.pop("story_engine", None)
    merged = _deep_merge(base, upgraded_payload)
    if not isinstance(merged.get("stage_states"), dict):
        merged["stage_states"] = _empty_stage_states()
    for stage_key in GENESIS_STAGE_ORDER:
        state = merged["stage_states"].get(stage_key)
        if not isinstance(state, dict):
            state = {}
        state.setdefault("stage_key", stage_key)
        state.setdefault("status", "todo")
        state.setdefault("locked", False)
        state.setdefault("updated_at", "")
        state.setdefault("last_trace_id", "")
        merged["stage_states"][stage_key] = state
    return merged


def _json_load_list_dicts(raw: str | None) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw or "[]") or []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)]
