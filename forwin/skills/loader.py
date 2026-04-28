from __future__ import annotations

import hashlib
from pathlib import Path

from .models import SkillCapability, SkillManifest
from .policy import ensure_skill_mode


def load_skill_manifest(path: str | Path, *, root: str | Path | None = None) -> SkillManifest:
    skill_path = Path(path)
    raw = skill_path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(raw, skill_path)
    mode = ensure_skill_mode(metadata.get("mode"))
    resolved_root = Path(root) if root is not None else skill_path.parent
    relative_path = _relative_path(skill_path, resolved_root)
    group = relative_path.split("/", 1)[0] if relative_path else ""
    return SkillManifest(
        name=str(metadata.get("name", "")).strip(),
        version=str(metadata.get("version", "1.0.0")).strip() or "1.0.0",
        description=str(metadata.get("description", "")).strip(),
        forwin_scope=str(metadata.get("forwin_scope", "")).strip(),
        stage_keys=tuple(_as_string_list(metadata.get("stage_keys"))),
        task_families=tuple(_as_string_list(metadata.get("task_families"))),
        mode=mode,
        body=body.strip(),
        path=relative_path,
        skill_hash=f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}",
        group=group,
        metadata=dict(metadata),
        capability=SkillCapability(mode=mode, instruction_only=(mode == "instruction_only")),
    )


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            return str(path.resolve())


def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, object], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Skill file is missing frontmatter: {path}")
    closing_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        raise ValueError(f"Skill file has unterminated frontmatter: {path}")
    metadata = _parse_frontmatter_lines(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1 :]).strip()
    if not str(metadata.get("name", "")).strip():
        raise ValueError(f"Skill file is missing name: {path}")
    if not str(metadata.get("forwin_scope", "")).strip():
        raise ValueError(f"Skill file is missing forwin_scope: {path}")
    return metadata, body


def _parse_frontmatter_lines(lines: list[str]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    current_key = ""
    current_nested_key = ""
    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent > 0:
            if not current_key:
                raise ValueError("Indented frontmatter line is missing a parent key")
            if stripped.startswith("- "):
                parent = metadata.setdefault(current_key, [])
                if isinstance(parent, list):
                    parent.append(_parse_scalar(stripped[2:].strip()))
                    continue
                if not isinstance(parent, dict):
                    raise ValueError(f"Frontmatter key {current_key} mixes scalar and list values")
                if not current_nested_key:
                    raise ValueError("Nested frontmatter list item is missing a key")
                values = parent.setdefault(current_nested_key, [])
                if not isinstance(values, list):
                    raise ValueError(
                        f"Nested frontmatter key {current_key}.{current_nested_key} mixes scalar and list values"
                )
                values.append(_parse_scalar(stripped[2:].strip()))
                continue
            parent = metadata.setdefault(current_key, {})
            if isinstance(parent, list) and not parent:
                parent = {}
                metadata[current_key] = parent
            if not isinstance(parent, dict):
                raise ValueError(f"Frontmatter key {current_key} mixes scalar and nested values")
            if ":" not in stripped:
                raise ValueError(f"Invalid nested frontmatter line: {line}")
            nested_key, raw_value = stripped.split(":", 1)
            current_nested_key = nested_key.strip()
            value = raw_value.strip()
            parent[current_nested_key] = [] if not value else _parse_scalar(value)
            continue
        if stripped.startswith("- "):
            if not current_key:
                raise ValueError("Frontmatter list item is missing a key")
            values = metadata.setdefault(current_key, [])
            if not isinstance(values, list):
                raise ValueError(f"Frontmatter key {current_key} mixes scalar and list values")
            values.append(_parse_scalar(stripped[2:].strip()))
            continue
        if ":" not in line:
            raise ValueError(f"Invalid frontmatter line: {line}")
        key, raw_value = line.split(":", 1)
        current_key = key.strip()
        current_nested_key = ""
        value = raw_value.strip()
        if not value:
            metadata[current_key] = []
            continue
        metadata[current_key] = _parse_scalar(value)
    return metadata


def _parse_scalar(value: str) -> object:
    stripped = value.strip()
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        return stripped[1:-1]
    lowered = stripped.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return stripped


def _as_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []
