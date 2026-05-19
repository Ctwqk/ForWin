from __future__ import annotations

import re
from pathlib import Path

from .trope_library import TropeTemplate


_TEMPLATE_HEADING_RE = re.compile(r"^##\s+([A-Za-z0-9_-]+)\s+·\s+(.+?)\s*$")
_H2_RE = re.compile(r"^##\s+")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")
_PROPERTY_RE = re.compile(r"^-\s+\*\*([^*]+)\*\*:\s*(.*?)\s*$")
_BULLET_RE = re.compile(r"^-\s+(.*?)\s*$")

_BODY_FIELD_BY_HEADING = {
    "欲望建立": "desire_setup",
    "阻力加压": "resistance",
    "爽点兑现": "payoff",
    "余波钩子": "aftermath",
}

_LIST_FIELD_BY_HEADING = {
    "anti_patterns": "anti_patterns",
    "review_signals": "review_signals",
}

_COMMA_LIST_FIELDS = {"genre_fit"}


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _coerce_property(key: str, value: str) -> object:
    normalized_key = key.strip()
    raw_value = value.strip()
    if normalized_key == "cost_weight":
        return int(raw_value)
    if normalized_key in _COMMA_LIST_FIELDS:
        return _split_csv(raw_value)
    return raw_value


def _heading_name(line: str) -> str:
    match = _H3_RE.match(line)
    return match.group(1).strip() if match else ""


def _section_text(lines: list[str]) -> str:
    return "\n".join(line.strip() for line in lines if line.strip()).strip()


def _section_bullets(lines: list[str]) -> list[str]:
    bullets: list[str] = []
    for line in lines:
        match = _BULLET_RE.match(line.strip())
        if match:
            item = match.group(1).strip()
            if item:
                bullets.append(item)
    return bullets


def _parse_template_section(template_id: str, display_name: str, lines: list[str]) -> TropeTemplate:
    payload: dict[str, object] = {
        "template_id": template_id.strip(),
        "display_name": display_name.strip(),
    }
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if _H3_RE.match(line):
            break
        property_match = _PROPERTY_RE.match(line)
        if property_match:
            key = property_match.group(1).strip()
            payload[key] = _coerce_property(key, property_match.group(2))
        index += 1

    while index < len(lines):
        line = lines[index].strip()
        if not _H3_RE.match(line):
            index += 1
            continue
        heading = _heading_name(line)
        index += 1
        body_lines: list[str] = []
        while index < len(lines) and not _H3_RE.match(lines[index].strip()):
            body_lines.append(lines[index])
            index += 1
        if heading in _BODY_FIELD_BY_HEADING:
            payload[_BODY_FIELD_BY_HEADING[heading]] = _section_text(body_lines)
        elif heading in _LIST_FIELD_BY_HEADING:
            payload[_LIST_FIELD_BY_HEADING[heading]] = _section_bullets(body_lines)

    return TropeTemplate.model_validate(payload)


def load_trope_templates_from_md(path: str | Path) -> tuple[TropeTemplate, ...]:
    markdown_path = Path(path)
    lines = markdown_path.read_text(encoding="utf-8").splitlines()
    templates: list[TropeTemplate] = []
    index = 0
    while index < len(lines):
        heading_match = _TEMPLATE_HEADING_RE.match(lines[index].strip())
        if not heading_match:
            index += 1
            continue
        template_id = heading_match.group(1).strip()
        display_name = heading_match.group(2).strip()
        index += 1
        section_lines: list[str] = []
        while index < len(lines) and not _H2_RE.match(lines[index].strip()):
            section_lines.append(lines[index])
            index += 1
        templates.append(_parse_template_section(template_id, display_name, section_lines))
    if not templates:
        raise ValueError(f"no trope templates found in markdown library: {markdown_path}")
    return tuple(templates)
