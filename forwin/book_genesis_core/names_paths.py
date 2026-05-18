from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *

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


__all__ = [name for name in globals() if not name.startswith("__")]
