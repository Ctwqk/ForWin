from __future__ import annotations

import re

GENESIS_STAGE_ORDER = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)

STAGE_TO_SECTION = {
    "brief": "book_brief",
    "world": "world",
    "map": "world.map_atlas",
    "story_engine": "world.story_engine",
    "book_blueprint": "book_arc_blueprint",
    "bootstrap": "execution_bootstrap",
}

GENESIS_STAGE_LABELS = {
    "brief": "创意简报",
    "world": "世界观与背景",
    "map": "地图与空间拓扑",
    "story_engine": "角色势力与叙事引擎",
    "book_blueprint": "整本书多 Arc 路线图",
    "bootstrap": "执行契约与启动交接",
}

WORLD_ROOT_KEYS = {
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

WORLD_BIBLE_KEYS = {
    "overview",
    "axioms",
    "history_slice",
    "naming_style",
    "forbidden_zones",
    "culture_profiles",
}

PATH_TOKEN_RE = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")

WORLD_STAGE_RELATIVE_PREFIXES = {
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

WORLD_STAGE_WORLD_BIBLE_ALIASES = {
    "overview",
    "axioms",
    "history_slice",
    "naming_style",
    "forbidden_zones",
    "culture_profiles",
}

WORLD_STAGE_STATE_KEYS = (
    "minimum_world_system",
    "minimum_extension_pack",
    "world_bible",
    "institution_profiles",
    "resource_economy_profiles",
    "world_extensions",
    "template_libraries",
)

