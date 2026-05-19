from __future__ import annotations

from typing import Any

from forwin.context.request import ContextDraft, ContextIssue, ContextRequest


class RecencyTruncateGate:
    name = "recency_truncate"

    def __init__(self, window_chapters: int = 0, max_entities: int = 0) -> None:
        self.window_chapters = max(0, int(window_chapters or 0))
        self.max_entities = max(0, int(max_entities or 0))

    def validate(self, request: ContextRequest, draft: ContextDraft) -> list[ContextIssue]:
        if self.window_chapters <= 0:
            return []

        current_chapter = int(getattr(request.chapter_plan, "chapter_number", 0) or 0)
        cutoff = current_chapter - self.window_chapters
        for key in ("summaries", "recent_state_changes", "recent_thread_beats", "recent_events"):
            self._trim_recent_items(draft.data, key, cutoff)
        if self.max_entities > 0:
            self._cap_entities(draft.data, cutoff)
        return []

    def _trim_recent_items(self, data: dict[str, Any], key: str, cutoff: int) -> None:
        value = data.get(key)
        if not isinstance(value, list):
            return
        trimmed = [item for item in value if self._is_recent_item(item, cutoff)]
        if len(trimmed) != len(value):
            data[key] = trimmed

    def _cap_entities(self, data: dict[str, Any], cutoff: int) -> None:
        value = data.get("entities")
        if not isinstance(value, list) or len(value) <= self.max_entities:
            return
        ranked = sorted(
            enumerate(value),
            key=lambda indexed: self._entity_rank(indexed[1], indexed[0], cutoff),
            reverse=True,
        )
        data["entities"] = [item for _, item in ranked[: self.max_entities]]

    def _entity_rank(self, item: Any, index: int, cutoff: int) -> tuple[bool, float, int, int]:
        last_seen = self._int_value(item, "last_seen_chapter", default=self._chapter_marker(item))
        importance = self._int_value(item, "importance", default=0)
        return (last_seen >= cutoff, importance, last_seen, -index)

    def _is_recent_item(self, item: Any, cutoff: int) -> bool:
        markers = self._chapter_markers(item)
        return not markers or any(marker >= cutoff for marker in markers)

    def _chapter_marker(self, item: Any) -> int | None:
        markers = self._chapter_markers(item)
        return max(markers) if markers else None

    def _chapter_markers(self, item: Any) -> list[int]:
        markers: list[int] = []
        for name in ("chapter_number", "last_seen_chapter"):
            value = self._raw_value(item, name)
            if value is not None:
                try:
                    markers.append(int(value))
                except (TypeError, ValueError):
                    continue
        return markers

    def _int_value(self, item: Any, name: str, *, default: int | None = None) -> int:
        value = self._raw_value(item, name)
        if value is None:
            value = default
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _raw_value(item: Any, name: str) -> Any:
        if isinstance(item, dict):
            return item.get(name)
        return getattr(item, name, None)
