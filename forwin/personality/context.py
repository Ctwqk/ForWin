from __future__ import annotations

from typing import Any

from .library import CharacterPersonalityLibrary
from .models import (
    ActivePersonalityContext,
    PersonalityBehaviorBias,
    PersonalityLoadout,
    PersonalitySkillInfo,
    PersonalitySkillRef,
)


_DEFAULT_CONSTRAINTS = [
    "Do not override canon.",
    "Do not make the character omniscient.",
    "Do not flatten the character into one repeated behavior.",
    "Do not infer behavior from model labels.",
]


def build_active_personality_context(
    *,
    character_id: str,
    character_name: str = "",
    loadout: PersonalityLoadout,
    library: CharacterPersonalityLibrary,
    scene_flags: list[str] | None = None,
    pressure_triggers: list[str] | None = None,
    relationship_targets: list[str] | None = None,
) -> ActivePersonalityContext:
    scene_flag_set = {str(item).strip() for item in (scene_flags or []) if str(item).strip()}
    pressure_set = {str(item).strip() for item in (pressure_triggers or []) if str(item).strip()}
    target_set = {str(item).strip() for item in (relationship_targets or []) if str(item).strip()}
    active = ActivePersonalityContext(
        character_id=character_id,
        character_name=character_name,
        constraints=list(_DEFAULT_CONSTRAINTS),
    )

    if loadout.dominant is not None:
        _append_skill(
            active,
            group="dominant",
            ref=loadout.dominant,
            skill=library.get(loadout.dominant.skill),
        )
    for ref in loadout.secondary:
        _append_skill(active, group="secondary", ref=ref, skill=library.get(ref.skill))
    for ref in loadout.social_mask:
        if _condition_matches(ref.active_when, scene_flag_set):
            _append_skill(active, group="social_mask", ref=ref, skill=library.get(ref.skill))
    for ref in loadout.relationship_patterns:
        if not ref.target or ref.target in target_set:
            _append_skill(active, group="relationship_pattern", ref=ref, skill=library.get(ref.skill))
    for ref in loadout.stress_modes:
        if _condition_matches(ref.trigger, pressure_set):
            _append_skill(active, group="stress_mode", ref=ref, skill=library.get(ref.skill))

    return active


def build_active_personality_contexts(
    characters: list[dict[str, Any]],
    *,
    library: CharacterPersonalityLibrary,
    scene_flags: list[str] | None = None,
    pressure_triggers: list[str] | None = None,
    relationship_targets_by_character: dict[str, list[str]] | None = None,
) -> list[ActivePersonalityContext]:
    contexts: list[ActivePersonalityContext] = []
    for character in characters:
        raw_loadout = character.get("personality_loadout") or {}
        if not raw_loadout:
            continue
        loadout = PersonalityLoadout.model_validate(raw_loadout)
        character_id = str(character.get("character_id") or character.get("id") or "").strip()
        if not character_id:
            continue
        contexts.append(
            build_active_personality_context(
                character_id=character_id,
                character_name=str(character.get("character_name") or character.get("name") or ""),
                loadout=loadout,
                library=library,
                scene_flags=scene_flags,
                pressure_triggers=pressure_triggers,
                relationship_targets=(relationship_targets_by_character or {}).get(character_id, []),
            )
        )
    return contexts


def _condition_matches(required: list[str], active: set[str]) -> bool:
    if not required:
        return True
    return bool(active.intersection(required))


def _append_skill(
    context: ActivePersonalityContext,
    *,
    group: str,
    ref: PersonalitySkillRef,
    skill: PersonalitySkillInfo | None,
) -> None:
    skill_id = skill.name if skill is not None else ref.skill
    active_skills = context.active_skills
    if group == "dominant":
        active_skills.dominant.append(skill_id)
    elif group == "secondary":
        active_skills.secondary.append(skill_id)
    elif group == "social_mask":
        active_skills.social_mask.append(skill_id)
    elif group == "stress_mode":
        active_skills.stress_mode.append(skill_id)
    elif group == "relationship_pattern":
        active_skills.relationship_pattern.append(skill_id)

    if skill is not None and _append_prompt_compression_bias(context.current_behavior_bias, group=group, skill=skill):
        return

    summary = _skill_summary(skill, ref)
    _append_bias(context.current_behavior_bias, group=group, summary=summary)


def _skill_summary(skill: PersonalitySkillInfo | None, ref: PersonalitySkillRef) -> str:
    if skill is None:
        return f"{ref.skill} is referenced but missing from the personality skill library."
    text = skill.description.strip() or _first_body_line(skill.body) or skill.name
    return text if ref.weight >= 0.99 else f"{text} (weight={ref.weight:.2f})"


def _first_body_line(body: str) -> str:
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.lower() in {"todo", "tbd"}:
            continue
        return line
    return ""


def _append_bias(bias: PersonalityBehaviorBias, *, group: str, summary: str) -> None:
    if group in {"dominant", "secondary"}:
        bias.perception.append(summary)
        bias.decision.append(summary)
    elif group == "social_mask":
        bias.dialogue.append(summary)
        bias.body_language.append(summary)
    elif group == "stress_mode":
        bias.stress_behavior.append(summary)
    elif group == "relationship_pattern":
        bias.relationship_behavior.append(summary)


def _append_prompt_compression_bias(
    bias: PersonalityBehaviorBias,
    *,
    group: str,
    skill: PersonalitySkillInfo,
) -> bool:
    compression = _extract_prompt_compression(skill.body)
    if not compression:
        return False
    added = False
    for key, target in (
        ("perception_bias", bias.perception),
        ("decision_bias", bias.decision),
        ("dialogue_bias", bias.dialogue),
        ("body_language_bias", bias.body_language),
        ("relationship_bias", bias.relationship_behavior),
        ("stress_bias", bias.stress_behavior),
    ):
        values = _as_string_list(compression.get(key))
        if values:
            target.extend(values)
            added = True
    if group == "social_mask":
        outward_values = _as_string_list(compression.get("outward_bias"))
        if outward_values:
            bias.dialogue.extend(outward_values)
            bias.body_language.extend(outward_values)
            added = True
        crack_values = _as_string_list(compression.get("crack_bias"))
        if crack_values:
            bias.stress_behavior.extend(crack_values)
            added = True
    if group == "stress_mode":
        for key in ("trigger_bias", "recovery_bias"):
            values = _as_string_list(compression.get(key))
            if values:
                bias.stress_behavior.extend(values)
                added = True
    return added


def _extract_prompt_compression(body: str) -> dict[str, object]:
    lines = str(body or "").splitlines()
    fenced_blocks: list[list[str]] = []
    in_fence = False
    current: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_fence:
                fenced_blocks.append(current)
                current = []
                in_fence = False
            else:
                in_fence = True
                current = []
            continue
        if in_fence:
            current.append(raw_line)
    for block in fenced_blocks:
        parsed = _parse_prompt_compression_block(block)
        if parsed:
            return parsed
    return _parse_prompt_compression_block(lines)


def _parse_prompt_compression_block(lines: list[str]) -> dict[str, object]:
    compression: dict[str, object] = {}
    in_prompt_compression = False
    current_key = ""
    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "prompt_compression:":
            in_prompt_compression = True
            current_key = ""
            continue
        if not in_prompt_compression:
            continue
        if indent == 0 and ":" in stripped and not stripped.startswith("- "):
            key, raw_value = stripped.split(":", 1)
            if key.strip() != "prompt_compression":
                break
            value = raw_value.strip()
            if value:
                return {}
            continue
        if ":" in stripped and not stripped.startswith("- "):
            key, raw_value = stripped.split(":", 1)
            current_key = key.strip()
            value = raw_value.strip()
            compression[current_key] = _strip_quotes(value) if value else []
            continue
        if stripped.startswith("- ") and current_key:
            values = compression.setdefault(current_key, [])
            if not isinstance(values, list):
                values = []
                compression[current_key] = values
            values.append(_strip_quotes(stripped[2:].strip()))
    return compression


def _as_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _strip_quotes(value: str) -> str:
    stripped = str(value or "").strip()
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        return stripped[1:-1]
    return stripped
