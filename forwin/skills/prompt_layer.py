from __future__ import annotations

from .models import SkillLayer, SkillSelection
from .policy import normalize_skill_strictness


_STRICTNESS_PREFIX = {
    "light": "参考以下工作流技能，优先保持兼容：",
    "normal": "遵守以下工作流技能要求：",
    "strict": "必须优先遵守以下工作流技能要求：",
}


class SkillPromptLayerBuilder:
    def __init__(self, *, strictness: str = "normal") -> None:
        self.strictness = normalize_skill_strictness(strictness)

    def build(self, selections: list[SkillSelection]) -> list[SkillLayer]:
        layers: list[SkillLayer] = []
        prefix = _STRICTNESS_PREFIX[self.strictness]
        for selection in selections:
            manifest = selection.manifest
            content = (
                f"{prefix}\n\n"
                f"# Skill: {manifest.name}\n"
                f"Version: {manifest.version}\n"
                f"Description: {manifest.description}\n\n"
                f"{manifest.body.strip()}"
            ).strip()
            layers.append(
                SkillLayer(
                    content=content,
                    skill_id=manifest.name,
                    skill_version=manifest.version,
                    skill_hash=manifest.skill_hash,
                    path=manifest.path,
                    activation_reason=selection.activation_reason,
                    mode=manifest.mode,
                )
            )
        return layers


def inject_skill_layers(
    messages: list[dict[str, str]],
    skill_layers: list[SkillLayer],
) -> list[dict[str, str]]:
    if not skill_layers:
        return [dict(item) for item in messages]
    result: list[dict[str, str]] = []
    index = 0
    while index < len(messages) and str(messages[index].get("role", "")).strip() == "system":
        result.append(dict(messages[index]))
        index += 1
    result.extend(layer.message_payload() for layer in skill_layers)
    result.extend(dict(item) for item in messages[index:])
    return result


def serialize_prompt_layers(
    messages: list[dict[str, str]],
    skill_layers: list[SkillLayer],
) -> list[dict[str, object]]:
    if not skill_layers:
        return [
            {
                "role": str(item.get("role", "")).strip(),
                "content": str(item.get("content", "")),
            }
            for item in messages
        ]
    payload: list[dict[str, object]] = []
    index = 0
    while index < len(messages) and str(messages[index].get("role", "")).strip() == "system":
        payload.append(
            {
                "role": str(messages[index].get("role", "")).strip(),
                "content": str(messages[index].get("content", "")),
            }
        )
        index += 1
    payload.extend(layer.trace_payload() for layer in skill_layers)
    payload.extend(
        {
            "role": str(item.get("role", "")).strip(),
            "content": str(item.get("content", "")),
        }
        for item in messages[index:]
    )
    return payload


def summarize_selected_skills(selections: list[SkillSelection]) -> list[dict[str, str]]:
    return [
        {
            "id": selection.manifest.name,
            "version": selection.manifest.version,
            "hash": selection.manifest.skill_hash,
            "path": selection.manifest.path,
            "activation_reason": selection.activation_reason,
            "mode": selection.manifest.mode,
        }
        for selection in selections
    ]
