from __future__ import annotations

import json
from typing import Any


LOCKED_FIELDS = [
    "Canon Summary",
    "Current State",
    "Relationships",
    "Reader Visibility",
    "Open Questions",
    "Evidence",
]
EDITABLE_FIELDS = ["Manual Notes", "Human Questions", "Proposed Correction"]


def dump_frontmatter(payload: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in payload.items():
        lines.extend(_dump_key_value(key, value))
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---\n"):
        return {}, markdown
    end = markdown.find("\n---", 4)
    if end < 0:
        return {}, markdown
    raw = markdown[4:end].strip("\n")
    body_start = markdown.find("\n", end + 4)
    body = markdown[body_start + 1 :] if body_start >= 0 else ""
    return _parse_yaml_subset(raw), body.lstrip("\n")


def parse_sections(markdown: str) -> dict[str, str]:
    _, body = parse_frontmatter(markdown)
    sections: dict[str, list[str]] = {}
    current = ""
    for line in body.splitlines():
        if line.startswith("## "):
            current = line.removeprefix("## ").strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def render_page(frontmatter: dict[str, Any], title: str, sections: dict[str, str]) -> str:
    body = [f"# {title}", ""]
    for key, value in sections.items():
        body.extend([f"## {key}", value.strip() if value.strip() else "_empty_", ""])
    return dump_frontmatter(frontmatter) + "\n".join(body).rstrip() + "\n"


def _dump_key_value(key: str, value: Any) -> list[str]:
    if isinstance(value, bool):
        return [f"{key}: {'true' if value else 'false'}"]
    if value is None:
        return [f"{key}: null"]
    if isinstance(value, (int, float)):
        return [f"{key}: {value}"]
    if isinstance(value, list):
        if not value:
            return [f"{key}: []"]
        lines = [f"{key}:"]
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"  - {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
            else:
                lines.append(f"  - {item}")
        return lines
    if isinstance(value, dict):
        return [f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}"]
    text = str(value).replace("\n", " ").strip()
    if any(ch in text for ch in [":", "#", "{", "}", "[", "]"]):
        return [f"{key}: {json.dumps(text, ensure_ascii=False)}"]
    return [f"{key}: {text}"]


def _parse_yaml_subset(raw: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    current_key = ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            payload.setdefault(current_key, [])
            item = line.removeprefix("  - ").strip()
            payload[current_key].append(_parse_scalar(item))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value == "":
            payload[key] = []
        else:
            payload[key] = _parse_scalar(value)
    return payload


def _parse_scalar(value: str) -> Any:
    if value in {"[]", "{}"}:
        return [] if value == "[]" else {}
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value.startswith(("{", "[", '"')):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip('"')
    try:
        return int(value)
    except ValueError:
        return value
