from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.names_paths import _culture_profile_generator_civilization, _generate_culture_names

def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item or "").strip(" 　，。；、,.《》「」“”")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _reject_placeholder_terms(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in _BLOCKED_PLACEHOLDER_TERMS:
            continue
        result.append(text)
    return result


_FALLBACK_SAMPLE_TERMS = {
    "陆明",
    "韩青",
    "主舞台",
    "主舞台文化",
    "主舞台总图",
    "主舞台核心区",
    "权力中心",
    "权力中心区",
    "危险边缘",
}


def _reject_sample_terms(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in _FALLBACK_SAMPLE_TERMS:
            continue
        result.append(text)
    return result


def _fallback_generated_names(*, civilization: str, kind: str, count: int, seed: str) -> list[str]:
    try:
        return _reject_sample_terms(
            _generate_culture_names(
                civilization=civilization,
                kind=kind,
                count=count,
                seed=seed,
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("Fallback culture naming failed for seed %s", seed, exc_info=True)
        return []


def _fallback_seed_text(pack: dict[str, Any]) -> str:
    book_brief = pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {}
    return "\n".join(
        str(part or "").strip()
        for part in (
            book_brief.get("premise"),
            book_brief.get("setting_seed"),
            book_brief.get("one_line"),
            book_brief.get("promise"),
        )
        if str(part or "").strip()
    )


def _fallback_named_entity_seed(pack: dict[str, Any]) -> dict[str, list[str]]:
    text = _fallback_seed_text(pack)
    character_names: list[str] = []
    for prefix in _FALLBACK_PERSON_ROLE_PREFIXES:
        pattern = re.compile(rf"{re.escape(prefix)}([\u4e00-\u9fff]{{2,3}}?)(?=在|，|、|和|与|。|$)")
        character_names.extend(match.group(1) for match in pattern.finditer(text))

    quoted_terms = _reject_placeholder_terms(re.findall(r"[「“《]([^」”》]{2,16})[」”》]", text))
    organizations = re.findall(
        r"([\u4e00-\u9fff]{2,12}(?:集团|公司|财团|企业|组织|协会|联盟|管理局|审计局|委员会|机构))(?=，|、|。|；|,|;|和|与|$)",
        text,
    )
    location_terms = list(quoted_terms)
    location_terms.extend(
        term
        for term in (
            "旧城",
            "核心系统",
            "地下检修线",
            "钟塔",
            "档案署",
            "中央广场",
            "旧城区",
            "旧港",
            "民间记忆馆",
            "地下数据市场",
        )
        if term in text
    )
    location_terms.extend(
        re.findall(
            r"([\u4e00-\u9fff]{2,12}(?:核心系统|地下线|检修线|星门|城邦|城市|钟塔|塔|楼|档案署|署|广场|旧城区|新区|港区|港口|记忆馆|档案馆|数据市场|市场|街区|码头|城区|边缘区))(?=，|、|。|；|,|;|并|是|为|和|与|$)",
            text,
        )
    )
    story_terms = re.findall(
        r"([\u4e00-\u9fff]{2,12}(?:账本|系统|火灾|遗书|循环|记忆))(?=，|、|。|；|,|;|和|与|$)",
        text,
    )
    return {
        "characters": _dedupe_preserve_order(_reject_placeholder_terms(character_names)),
        "locations": _dedupe_preserve_order(_reject_placeholder_terms(location_terms)),
        "organizations": _dedupe_preserve_order(_reject_placeholder_terms(organizations)),
        "story_terms": _dedupe_preserve_order(_reject_placeholder_terms(story_terms + quoted_terms)),
    }


def _fallback_location_kind(name: str) -> str:
    text = str(name or "")
    if "星门" in text:
        return "gate"
    if "城邦" in text or "城市" in text:
        return "city"
    if "公会" in text or "档案" in text:
        return "institution"
    if "塔" in text or "楼" in text:
        return "landmark"
    if "轨" in text or "码头" in text or "港" in text:
        return "transit"
    if "广场" in text:
        return "public_square"
    if "苑" in text or "馆" in text:
        return "site"
    return "location"


def _fallback_culture_profiles(project: Project, pack: dict[str, Any]) -> list[dict[str, Any]]:
    seed_entities = _fallback_named_entity_seed(pack)
    overview = str(project.setting_summary or _fallback_seed_text(pack) or project.genre or "项目世界").strip()
    title = str(getattr(project, "title", "") or "").strip()
    anchor = next(
        (
            item
            for item in (
                seed_entities["locations"]
                + seed_entities["organizations"]
                + seed_entities["story_terms"]
                + ([title] if title else [])
            )
            if item
        ),
        "项目世界",
    )
    civilization = "中华"
    seed_prefix = f"{title or anchor}:fallback-culture"
    character_examples = _dedupe_preserve_order(
        _reject_sample_terms(seed_entities["characters"])
        + _fallback_generated_names(
            civilization=civilization,
            kind="person",
            count=5,
            seed=f"{seed_prefix}:person",
        )
    )[:5]
    region_examples = _dedupe_preserve_order(
        _reject_sample_terms(seed_entities["locations"])
        + _fallback_generated_names(
            civilization=civilization,
            kind="region",
            count=5,
            seed=f"{seed_prefix}:region",
        )
    )[:5]
    location_examples = _dedupe_preserve_order(
        _reject_sample_terms(seed_entities["locations"] + seed_entities["story_terms"])
        + _fallback_generated_names(
            civilization=civilization,
            kind="place",
            count=5,
            seed=f"{seed_prefix}:place",
        )
    )[:5]
    return [
        {
            "id": "culture-project-seed",
            "name": f"{anchor}文化",
            "summary": f"围绕{overview}形成的项目专属命名与文化语感。",
            "inspiration": overview,
            "generator_civilization": civilization,
            "generator_overlays": [],
            "social_markers": ["重秩序", "重门第", "旧俗与新制并行"],
            "aesthetic_keywords": ["冷色秩序", "旧城感", "压抑繁荣"],
            "character_name_style": "人物名以两到三字为主，简洁、冷硬、易记。",
            "region_name_style": "地区名强调功能、灾变压力或制度层级。",
            "location_name_style": "地点名强调辨识度、叙事用途和项目专属设定。",
            "character_name_examples": character_examples,
            "region_name_examples": region_examples,
            "location_name_examples": location_examples,
            "usage_notes": "仅作为本项目 deterministic fallback 的命名提示，不作为跨项目 canon。",
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
    world = _pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {}
    overview = str(world.get("overview", "") or "")
    culture_profiles = [item for item in (world.get("culture_profiles") or []) if isinstance(item, dict)]
    primary_profile = (culture_profiles[0] if culture_profiles else {}) or {}
    primary_culture_id = str(primary_profile.get("id", "") or "culture-main-stage") if culture_profiles else "culture-main-stage"
    seed_entities = _fallback_named_entity_seed(pack)
    locations = seed_entities["locations"]
    organizations = seed_entities["organizations"]
    primary_subworld_name = "项目总图"
    primary_region_name = "核心叙事区"
    power_region_name = "制度中枢区"
    primary_node_name = "核心舞台"
    power_node_name = organizations[0] if organizations else "治理中枢"
    danger_node_name = "边境外缘"
    civilization = _culture_profile_generator_civilization(primary_profile)
    if locations:
        stage_root = "旧城" if "旧城" in locations else locations[0]
        core_locations = [item for item in locations if item != stage_root]
        primary_subworld_name = stage_root
        primary_region_name = next((item for item in locations if "区" in item), f"{stage_root}核心区")
        primary_node_name = next(
            (
                item
                for item in core_locations
                if any(marker in item for marker in ("核心系统", "钟塔", "记忆馆", "档案馆"))
            ),
            core_locations[0] if core_locations else stage_root,
        )
        power_node_name = next(
            (
                item
                for item in core_locations
                if any(marker in item for marker in ("公会", "系统", "管理局", "审计局"))
            ),
            organizations[0] if organizations else primary_node_name,
        )
        danger_node_name = next(
            (
                item
                for item in core_locations
                if any(marker in item for marker in ("检修线", "市场", "港", "广场", "边缘"))
            ),
            core_locations[-1] if len(core_locations) > 1 else f"{stage_root}外缘",
        )
        if danger_node_name in _FALLBACK_SAMPLE_TERMS:
            danger_node_name = f"{stage_root}外缘"
        if organizations:
            power_region_name = organizations[0]
            if power_node_name == primary_node_name:
                power_node_name = organizations[0]
    elif civilization:
        generated_region_names = _fallback_generated_names(
            civilization=civilization,
            kind="region",
            count=3,
            seed=f"{primary_culture_id}:map-regions",
        )
        generated_place_names = _fallback_generated_names(
            civilization=civilization,
            kind="place",
            count=3,
            seed=f"{primary_culture_id}:map-nodes",
        )
        if len(generated_region_names) >= 3:
            primary_subworld_name, primary_region_name, power_region_name = generated_region_names[:3]
        if len(generated_place_names) >= 3:
            primary_node_name, power_node_name, danger_node_name = generated_place_names[:3]
    governing_power = organizations[0] if organizations else f"{primary_subworld_name}治理中枢"
    primary_region_id = "region-main-stage"
    power_region_id = "region-power-core"
    primary_subworld_id = "subworld-main-stage"
    primary_node_id = "node-main-stage"
    power_node_id = "node-power-core"
    danger_node_id = "node-danger-edge"
    selected_node_names = {primary_node_name, power_node_name, danger_node_name}
    extra_location_nodes = [
        {
            "id": f"node-seed-location-{index}",
            "name": name,
            "kind": _fallback_location_kind(name),
            "parent_subworld": primary_subworld_id,
            "parent_region_id": primary_region_id,
            "culture_profile_id": primary_culture_id,
            "description": f"{name}是从项目设定中保留下来的核心舞台，用于承接线索、移动成本或权力压力。",
            "control": "多方关注",
            "danger": "中等，具体风险由章节推进细化。",
            "climate_note": "沿用本区域气候。",
            "terrain_note": "与旧城空间网络相连。",
            "culture_note": "承接项目文化语感。",
            "resources": ["线索", "档案", "关系压力"],
        }
        for index, name in enumerate(
            [
                item
                for item in locations
                if item not in selected_node_names and item != primary_subworld_name
            ][:8],
            start=1,
        )
    ]
    key_location_names = _dedupe_preserve_order(
        [primary_node_name, power_node_name, danger_node_name]
        + [str(item.get("name", "")).strip() for item in extra_location_nodes]
    )
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
                "governing_power": governing_power,
                "resident_factions": [governing_power],
                "key_locations": key_location_names,
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
                "controller_factions": [governing_power],
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
                "summary": "项目核心秩序与权力最集中的地区。",
                "culture_traits": ["等级森严", "血统叙事强"],
                "climate": "城市微气候稳定。",
                "terrain": ["高密度城建", "旧城区"],
                "controller_factions": [governing_power],
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
                "control": governing_power,
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
                "control": governing_power,
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
        ]
        + extra_location_nodes,
        "edges": [
            {"from": primary_node_name, "to": power_node_name, "relation": "常规往返"},
            {"from": primary_node_name, "to": danger_node_name, "relation": "高风险探索"},
        ]
        + [
            {"from": primary_node_name, "to": str(item.get("name", "")).strip(), "relation": "线索通道"}
            for item in extra_location_nodes
            if str(item.get("name", "")).strip()
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
    seed_entities = _fallback_named_entity_seed(pack)
    character_names = seed_entities["characters"]
    organizations = seed_entities["organizations"]
    protagonist_name = "主角"
    primary_faction = "项目权力中枢"
    primary_faction_id = "faction-main-stage"
    opposition_name = "对手盘"
    civilization = _culture_profile_generator_civilization(primary_profile)
    if character_names:
        protagonist_name = character_names[0]
    if organizations:
        primary_faction = organizations[0]
        opposition_name = organizations[0]
    elif civilization:
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
    cast_names = character_names or [protagonist_name]
    core_cast = []
    for index, name in enumerate(cast_names[:6]):
        core_cast.append(
            {
                "name": name,
                "role": "主视角" if index == 0 else "关键盟友",
                "desire": "摆脱被动处境并掌握真相" if index == 0 else "协助主角补全真相并守住自身动机",
                "fear": "在升级过程中失去最重要的人或自我",
                "secret": "与旧时代规则存在未公开的深层关联。" if index == 0 else "掌握一段尚未公开的线索或利益牵连。",
                "culture_profile_id": primary_culture_id,
                "home_subworld": primary_subworld,
                "home_region": primary_region_id,
                "home_location": primary_location,
                "current_region": primary_region_id,
                "current_base": primary_location,
                "affiliated_faction": primary_faction_id,
                "affiliated_family": "主角原生家庭" if index == 0 else "",
                "faction_memberships": [
                    {
                        "faction_name": primary_faction,
                        "relation": "member" if index == 0 else "ally",
                        "rank": "外围关联者" if index == 0 else "协作对象",
                        "is_primary": True,
                    }
                ],
            }
        )
    return {
        "core_cast": core_cast,
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
    story_focus = _clean_story_focus_from_pack(project, pack)
    phase_labels = ("触发线索", "深入追查", "真相反转", "系统逼近", "终局抉择")
    for index, chapter_count in enumerate(sizes, start=1):
        chapter_start = chapter_cursor
        chapter_end = chapter_cursor + chapter_count - 1
        phase = phase_labels[min(index - 1, len(phase_labels) - 1)]
        arcs.append(
            {
                "arc_number": index,
                "title": f"Arc {index}",
                "arc_synopsis": f"{phase}：围绕“{story_focus}”推进第 {index} 段核心冲突，并拉高下一段压力。",
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




__all__ = [name for name in globals() if not name.startswith("__")]
