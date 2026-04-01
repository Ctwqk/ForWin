from __future__ import annotations

import json
import logging
from typing import Any

from forwin.writer.llm_client import LLMClient
from forwin.utils import parse_llm_json

logger = logging.getLogger(__name__)


class ArcDirector:
    """Plans the initial arc independently from chapter writing.

    Phase 0.5 defaults to smaller, bounded JSON calls so MiniMax M2.7 does not
    spend minutes in long reasoning mode before returning a giant object.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        max_tokens: int = 16384,
    ) -> None:
        self.llm_client = llm_client
        self.max_tokens = max_tokens

    def plan_arc(
        self,
        premise: str,
        genre: str,
        num_chapters: int = 3,
    ) -> dict:
        logger.info(
            "ArcDirector.plan_arc: premise_len=%d genre=%r num_chapters=%d",
            len(premise),
            genre,
            num_chapters,
        )
        core = self._plan_core(premise, genre)
        chapters = self._plan_chapters(premise, genre, num_chapters, core)
        world = self._build_world_scaffold(premise, genre, core, chapters)

        result = {
            "arc_synopsis": str(core.get("arc_synopsis", "")).strip(),
            "setting_summary": str(core.get("setting_summary", "")).strip(),
            "initial_time": core.get("initial_time") or {
                "label": "现代雨季",
                "description": "故事开始于一场持续不断的雨夜。",
            },
            "chapters": self._normalize_chapters(chapters, num_chapters, premise),
            "characters": self._as_list(world.get("characters")),
            "locations": self._as_list(world.get("locations")),
            "factions": self._as_list(world.get("factions")),
            "relations": self._as_list(world.get("relations")),
            "plot_threads": self._as_list(world.get("plot_threads")),
        }
        logger.info("ArcDirector.plan_arc: parsed ok – keys=%s", list(result.keys()))
        return result

    def _build_world_scaffold(
        self,
        premise: str,
        genre: str,
        core: dict,
        chapters: list[dict],
    ) -> dict:
        """Phase 0.5 keeps world planning deliberately lightweight.

        The current priority is to keep `generate -> write -> persist` stable.
        A deterministic scaffold is preferable to burning multiple LLM calls on
        front-loaded worldbuilding before we have even produced chapter one.
        """
        plot_name = f"{genre}主线"
        plot_description = str(core.get("arc_synopsis", "")).strip() or premise[:120]
        return {
            "characters": [],
            "locations": [],
            "factions": [],
            "relations": [],
            "plot_threads": [
                {
                    "name": plot_name,
                    "description": plot_description,
                    "status": "active",
                    "priority": 10,
                }
            ],
        }

    def _plan_core(self, premise: str, genre: str) -> dict:
        fallback = {
            "arc_synopsis": (
                f"《{genre}》故事围绕“{premise}”展开。主角会在不断升级的危机里逼近真相，"
                "并在结尾做出一次高代价抉择。"
            ),
            "setting_summary": "故事发生在现代城市与隐秘超常规则并存的世界里，表层日常之下埋着危险秘密。",
            "initial_time": {
                "label": "故事开始的第一场雨夜",
                "description": "一切异变都从这个夜晚开始。",
            },
        }
        prompt = [
            {
                "role": "system",
                "content": "你是中文网文编辑，只输出 JSON 对象，不要 markdown，不要解释。",
            },
            {
                "role": "user",
                "content": (
                    "请为下面的网文生成一个精简故事核心设定，只返回 JSON。\n"
                    "字段必须包含：arc_synopsis、setting_summary、initial_time。\n"
                    "其中 arc_synopsis 控制在 2 段内，setting_summary 控制在 120 字内，"
                    "initial_time 必须是一个含 label/description 的对象。\n\n"
                    f"类型：{genre}\n"
                    f"前提：{premise}"
                ),
            },
        ]
        return self._call_json(
            prompt,
            temperature=0.5,
            max_tokens=min(self.max_tokens, 900),
            fallback=fallback,
        )

    def _plan_chapters(
        self,
        premise: str,
        genre: str,
        num_chapters: int,
        core: dict,
    ) -> list[dict]:
        fallback = {
            "chapters": [
                {
                    "chapter_number": index,
                    "title": f"第{index}章",
                    "one_line": f"围绕“{premise[:18]}”推进新的危机与线索。",
                    "goals": [
                        "推进本章主线冲突",
                        "让主角获得新的信息或代价",
                    ],
                }
                for index in range(1, num_chapters + 1)
            ]
        }
        prompt = [
            {
                "role": "system",
                "content": "你是章节策划编辑，只输出 JSON 对象。",
            },
            {
                "role": "user",
                "content": (
                    f"请为一个 {genre} 网文规划恰好 {num_chapters} 章的章节表，只返回 JSON。\n"
                    "顶层格式：{\"chapters\": [...]}。\n"
                    "每个 chapter 必须包含：chapter_number、title、one_line、goals。\n"
                    "goals 必须是 2 到 3 条短句。\n\n"
                    f"故事前提：{premise}\n"
                    f"整体弧线：{core.get('arc_synopsis', '')}\n"
                    f"世界背景：{core.get('setting_summary', '')}"
                ),
            },
        ]
        payload = self._call_json(
            prompt,
            temperature=0.45,
            max_tokens=min(self.max_tokens, 1200),
            fallback=fallback,
        )
        return self._as_list(payload.get("chapters"))

    def _plan_world(
        self,
        premise: str,
        genre: str,
        core: dict,
        chapters: list[dict],
    ) -> dict:
        fallback = {
            "characters": [],
            "locations": [],
            "factions": [],
            "relations": [],
            "plot_threads": [],
        }
        chapter_brief = "\n".join(
            f"- 第{item.get('chapter_number', idx + 1)}章：{item.get('title', '')}｜{item.get('one_line', '')}"
            for idx, item in enumerate(chapters[:6])
        )
        prompt = [
            {
                "role": "system",
                "content": "你是故事世界设定编辑，只输出 JSON 对象。",
            },
            {
                "role": "user",
                "content": (
                    "请为这个故事补齐初始角色、地点、势力、关系和剧情线，只返回 JSON。\n"
                    "顶层必须包含：characters、locations、factions、relations、plot_threads。\n"
                    "每个数组最多 4 项，避免过度展开。角色 initial_state 使用固定字段 "
                    "location/status/goal/power_level/mood；地点 initial_state 使用 "
                    "status/controlled_by；势力 initial_state 使用 status/location/goal/power_level。\n\n"
                    f"类型：{genre}\n"
                    f"前提：{premise}\n"
                    f"整体弧线：{core.get('arc_synopsis', '')}\n"
                    f"章节表：\n{chapter_brief}"
                ),
            },
        ]
        return self._call_json(
            prompt,
            temperature=0.4,
            max_tokens=min(self.max_tokens, 1400),
            fallback=fallback,
        )

    def _call_json(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        fallback: dict,
    ) -> dict:
        attempts = [
            {"temperature": temperature, "max_tokens": max_tokens},
            {"temperature": max(0.2, temperature - 0.15), "max_tokens": max(480, min(max_tokens, 900))},
            {"temperature": 0.2, "max_tokens": max(420, min(max_tokens, 700))},
        ]
        last_error: Exception | None = None
        for index, attempt in enumerate(attempts, start=1):
            try:
                try:
                    raw = self.llm_client.chat(
                        messages,
                        temperature=attempt["temperature"],
                        max_tokens=attempt["max_tokens"],
                        response_format={"type": "json_object"},
                    )
                except TypeError as exc:
                    if "response_format" not in str(exc):
                        raise
                    raw = self.llm_client.chat(
                        messages,
                        temperature=attempt["temperature"],
                        max_tokens=attempt["max_tokens"],
                    )
                return parse_llm_json(raw, error_prefix="ArcDirector JSON parser")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "ArcDirector JSON call failed on attempt %d/%d: %s",
                    index,
                    len(attempts),
                    exc,
                )
                if "First 300 chars: ''" in str(exc):
                    break
        logger.warning("ArcDirector falling back to deterministic scaffold: %s", last_error)
        return fallback

    @staticmethod
    def _as_list(value: Any) -> list[dict]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def _normalize_chapters(
        self,
        items: list[dict],
        num_chapters: int,
        premise: str,
    ) -> list[dict]:
        normalized: list[dict] = []
        for index in range(1, num_chapters + 1):
            source = items[index - 1] if index - 1 < len(items) else {}
            title = str(source.get("title", "")).strip() or f"第{index}章"
            one_line = (
                str(source.get("one_line", "")).strip()
                or f"围绕“{premise[:18]}”推进新一轮变化。"
            )
            raw_goals = source.get("goals")
            goals = [
                str(item).strip()
                for item in (raw_goals if isinstance(raw_goals, list) else [])
                if str(item).strip()
            ][:3]
            if len(goals) < 2:
                goals = [
                    "推进本章主线冲突",
                    "提供新的线索、代价或反转",
                ]
            normalized.append(
                {
                    "chapter_number": index,
                    "title": title,
                    "one_line": one_line,
                    "goals": goals,
                }
            )
        return normalized
