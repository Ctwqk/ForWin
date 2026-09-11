"""The bounded world view shared by Writer prompts and their budget estimate."""

from __future__ import annotations

from forwin.obsidian.frontmatter import (
    frontmatter_hidden,
    parse_frontmatter,
    parse_sections,
)
from forwin.protocol.book_state import WorldNodeType
from forwin.protocol.world_model import WorldContextPack, WorldModelPage


def _writer_page_frontmatter(page: WorldModelPage) -> dict | None:
    embedded, _ = parse_frontmatter(page.markdown)
    if any(
        frontmatter_hidden(metadata)
        for metadata in (
            embedded,
            page.frontmatter,
            {"node_type": page.page_type, "status": page.status},
        )
    ):
        return None
    return {**embedded, **page.frontmatter}


def render_world_context(world_context: WorldContextPack | None) -> str | None:
    if not world_context or not world_context.snapshot_id:
        return None
    lines = [
        "【WorldModel 当前世界状态】",
        f"  · snapshot：第 {world_context.as_of_chapter} 章后 / {world_context.snapshot_id}",
    ]
    if world_context.active_world_conflicts:
        lines.append("  · 禁止忽略的世界矛盾：")
        lines.extend(
            f"    - {item.severity} {item.conflict_type}：{item.description}"
            for item in world_context.active_world_conflicts[:4]
        )
    visible_pages = [
        (page, metadata)
        for page in world_context.relevant_world_pages
        if (metadata := _writer_page_frontmatter(page)) is not None
    ]
    if visible_pages:
        lines.append("  · 相关世界页：")
        lines.append(
            "    truth_relation=false/unknown 不作客观真相；读者可见不代表所有人物已知。"
        )
        for page, metadata in visible_pages[:6]:
            sections = parse_sections(page.markdown)
            individual = page.page_type in {item.value for item in WorldNodeType} | {
                "map_node"
            } and (
                metadata.get("node_id")
                or (
                    page.canonical_source_type == "book_state_node"
                    and page.canonical_source_id
                )
            )
            section_fields = [("Canon Summary", "概要")]
            if individual:
                section_fields.append(("Current State", "状态"))
            parts = [
                (label, " ".join(sections.get(key, "").split()))
                for key, label in section_fields
                if sections.get(key, "").strip() not in {"", "_empty_", "_none_"}
            ]
            if not parts:
                continue
            # Share the existing per-page text allowance so state cannot be
            # crowded out by either frontmatter or a long summary.
            limit = 220 // len(parts)
            content = "；".join(f"{label}：{text[:limit]}" for label, text in parts)
            visibility = metadata.get("visibility", "unknown")
            truth = metadata.get("truth_relation", "unknown")
            lines.append(
                f"    - {page.title}（{page.page_type}；visibility={visibility}；"
                f"truth_relation={truth}）：{content}"
            )
    visible_promises = [
        page
        for page in world_context.active_promises
        if _writer_page_frontmatter(page) is not None
    ]
    if visible_promises:
        lines.append("  · 当前读者承诺：")
        lines.extend(f"    - {page.title}" for page in visible_promises[:4])
    if world_context.active_secrets:
        lines.append(
            "  · 秘密可见性：不得提前揭示 secret 页面中的 hidden truth，除非本章计划明确要求。"
        )
    return "\n".join(lines)
