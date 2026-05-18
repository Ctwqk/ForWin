from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.fallbacks import *
from forwin.book_genesis_core.names_paths import *

def _refine_support_context(self, *, pack: dict[str, Any], stage_key: str) -> dict[str, Any]:
    if stage_key == "brief":
        return {}
    if stage_key == "world":
        return {"book_brief": pack.get("book_brief") or {}, "world": _pack_stage_payload(pack, "world")}
    if stage_key == "map":
        world_root = _pack_stage_payload(pack, "world")
        return {
            "book_brief": pack.get("book_brief") or {},
            "world_bible": world_root.get("world_bible") or {},
            "naming_assist": _name_hint_block(
                world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {},
                seed_prefix="refine:map",
            ),
        }
    if stage_key == "story_engine":
        world_root = _pack_stage_payload(pack, "world")
        return {
            "book_brief": pack.get("book_brief") or {},
            "world_bible": world_root.get("world_bible") or {},
            "map_atlas": world_root.get("map_atlas") or {},
            "naming_assist": _name_hint_block(
                world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {},
                seed_prefix="refine:story_engine",
            ),
        }
    if stage_key == "book_blueprint":
        world_root = _pack_stage_payload(pack, "world")
        return {
            "book_brief": pack.get("book_brief") or {},
            "world_bible": world_root.get("world_bible") or {},
            "map_atlas": world_root.get("map_atlas") or {},
            "story_engine": world_root.get("story_engine") or {},
        }
    return pack

def _plan_arc_chapters(
    self,
    *,
    project: Project,
    pack: dict[str, Any],
    arc_payload: dict[str, Any],
    chapter_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fallback = [
        {
            "title": f"第{index}章",
            "one_line": f"围绕“{arc_payload.get('arc_synopsis', project.premise)[:28]}”推进冲突。",
            "goals": ["推进当前 arc 主线", "制造新线索或新代价"],
        }
        for index in range(1, chapter_count + 1)
    ]
    messages = [
        {"role": "system", "content": "你是 Arc 细化编辑，只输出 JSON 对象。"},
        {
            "role": "user",
            "content": (
                f"请为当前 arc 规划恰好 {chapter_count} 章，只返回 JSON，顶层格式为 "
                "{\"chapters\": [...]}，每项包含 title、one_line、goals。\n\n"
                f"BookBrief：{_json_dump(pack.get('book_brief') or {})}\n"
                f"WorldBible：{_json_dump(_pack_stage_payload(pack, 'world').get('world_bible') or {})}\n"
                f"StoryEngine：{_json_dump(_pack_stage_payload(pack, 'story_engine') or {})}\n"
                f"当前 Arc：{_json_dump(arc_payload)}"
            ),
        },
    ]
    payload, trace_payload = self._call_json_with_trace(
        messages=messages,
        fallback={"chapters": fallback},
        stage_key=f"launch_arc_{int(arc_payload.get('arc_number', 1) or 1)}",
        max_tokens=1200,
    )
    chapters = payload.get("chapters") if isinstance(payload, dict) else []
    normalized: list[dict[str, Any]] = []
    for index in range(1, chapter_count + 1):
        source = chapters[index - 1] if index - 1 < len(chapters) and isinstance(chapters[index - 1], dict) else {}
        goals = [
            str(item).strip()
            for item in (source.get("goals") or [])
            if str(item).strip()
        ][:3]
        normalized.append(
            {
                "title": str(source.get("title", "")).strip() or fallback[index - 1]["title"],
                "one_line": str(source.get("one_line", "")).strip() or fallback[index - 1]["one_line"],
                "goals": goals or fallback[index - 1]["goals"],
            }
        )
    return normalized, trace_payload




__all__ = ['_refine_support_context', '_plan_arc_chapters']
