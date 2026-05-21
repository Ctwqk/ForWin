from __future__ import annotations

from forwin.book_genesis_core.constants import *

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


def _prompt_render(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return _json_dump(value)
    if isinstance(value, tuple):
        return _json_dump(list(value))
    if value is None:
        return ""
    return str(value).strip()


def _prompt_bullets(lines: list[str] | tuple[str, ...]) -> str:
    return "\n".join(
        f"- {str(line).strip()}"
        for line in lines
        if str(line).strip()
    )


def _prompt_sections(*sections: tuple[str, Any]) -> str:
    blocks: list[str] = []
    for title, content in sections:
        rendered = _prompt_render(content)
        if not rendered:
            continue
        blocks.append(f"【{title}】\n{rendered}")
    return "\n\n".join(blocks)


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


def _world_stage_state_view(world_root: dict[str, Any] | None) -> dict[str, Any]:
    payload = world_root if isinstance(world_root, dict) else {}
    return {
        key: _json_clone(payload.get(key))
        for key in _WORLD_STAGE_STATE_KEYS
    }


def _locked_stage_context(pack: dict[str, Any], current_stage_key: str) -> list[dict[str, Any]]:
    if current_stage_key not in GENESIS_STAGE_ORDER:
        return []
    stage_states = pack.get("stage_states") if isinstance(pack.get("stage_states"), dict) else {}
    current_index = GENESIS_STAGE_ORDER.index(current_stage_key)
    context: list[dict[str, Any]] = []
    for stage_key in GENESIS_STAGE_ORDER[:current_index]:
        state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
        if not bool(state.get("locked")):
            continue
        context.append(
            {
                "stage_key": stage_key,
                "stage_label": _GENESIS_STAGE_LABELS.get(stage_key, stage_key),
                "updated_at": str(state.get("updated_at", "") or ""),
                "payload": _pack_stage_payload(pack, stage_key),
            }
        )
    return context


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


_PREMISE_META_PREFIXES = (
    "本书用于",
    "生成日期",
    "依据文件",
    "目标：",
    "目标:",
    "质量要求：",
    "质量要求:",
    "创作约束：",
    "创作约束:",
    "注意：",
    "注意:",
)


def _premise_field(premise: str, label: str) -> str:
    pattern = re.compile(
        rf"{re.escape(label)}\s*[:：]\s*(.+?)(?=\n\S+\s*[:：]|$)",
        re.S,
    )
    match = pattern.search(premise)
    return re.sub(r"\s+", " ", match.group(1)).strip(" 。；;") if match else ""


def _fallback_story_premise_excerpt(project: Project) -> str:
    premise = str(project.premise or "").strip()
    conflict = _premise_field(premise, "核心冲突")
    if conflict:
        return conflict[:96]

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in premise.splitlines()
        if line.strip()
    ]
    story_lines = [
        line
        for line in lines
        if not any(line.startswith(prefix) for prefix in _PREMISE_META_PREFIXES)
    ]
    source = story_lines[0] if story_lines else premise
    return source[:96]


def _extract_story_focus_text(value: str) -> str:
    focus = re.sub(r"\s+", " ", str(value or "")).strip(" 。；;")
    if not focus:
        return ""

    wrapped = re.search(r"围绕[“\"](.+?)[”\"]展开", focus)
    if wrapped:
        focus = wrapped.group(1)
    else:
        wrapped = re.search(r"围绕[“\"](.+?)[”\"]", focus)
        if wrapped:
            focus = wrapped.group(1)

    focus = re.sub(r"^\S{0,16}长篇[，,]\s*", "", focus)
    return re.sub(r"\s+", " ", focus).strip(" 。；;")


def _clean_story_focus_from_pack(project: Project, pack: dict[str, Any]) -> str:
    book_brief = pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {}
    focus = _extract_story_focus_text(str(book_brief.get("one_line") or ""))
    if not focus:
        focus = _fallback_story_premise_excerpt(project)
    return focus[:96]


def _story_focus_from_blueprint_fallback(project: Project, fallback: dict[str, Any]) -> str:
    fallback_arcs = [item for item in (fallback.get("arcs") or []) if isinstance(item, dict)]
    synopsis = str((fallback_arcs[0] if fallback_arcs else {}).get("arc_synopsis") or "").strip()
    match = re.search(r"围绕“(.+?)”", synopsis)
    focus = _extract_story_focus_text(match.group(1) if match else synopsis)
    return focus[:96] or _fallback_story_premise_excerpt(project)


def _fallback_brief(project: Project, book_brief: dict[str, Any]) -> dict[str, Any]:
    story_excerpt = _fallback_story_premise_excerpt(project)
    genre_label = str(project.genre or "").strip() or "长篇"
    if not genre_label.endswith("长篇"):
        genre_label = f"{genre_label}长篇"
    return {
        "title": project.title,
        "premise": book_brief.get("premise") or project.premise,
        "genre": book_brief.get("genre") or project.genre,
        "target_total_chapters": int(
            book_brief.get("target_total_chapters") or project.target_total_chapters or 1
        ),
        "setting_seed": book_brief.get("setting_seed") or project.setting_summary,
        "one_line": f"{genre_label}，围绕“{story_excerpt}”展开。",
        "audience": book_brief.get("audience_hint") or "网文读者",
        "core_emotion": book_brief.get("core_emotion") or "紧张与上升",
        "core_delight": book_brief.get("core_delight") or "危机升级、线索反转、主角成长",
        "promise": book_brief.get("narrative_promise") or "持续升级、逐步揭示世界真相。",
        "guardrails": book_brief.get("content_guardrails") or [],
    }


_FALLBACK_PERSON_ROLE_PREFIXES = (
    "主角：",
    "主角",
    "主人公：",
    "主人公",
    "失业档案修复师",
    "前调查记者",
    "企业继承人",
    "地下算法师",
    "失忆警员",
    "档案修复师",
    "调查记者",
    "继承人",
    "算法师",
    "警员",
    "记者",
    "修复师",
)
_BLOCKED_PLACEHOLDER_TERMS = {
    "相关人员",
    "一名相关人员",
}




__all__ = [name for name in globals() if not name.startswith("__")]
