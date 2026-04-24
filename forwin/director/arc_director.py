from __future__ import annotations

import json
import logging
from typing import Any

from forwin.arc_sizing import allocate_arc_chapter_sizes
from forwin.writer.llm_client import LLMClient
from forwin.utils import LLMJSONParseError, parse_llm_json

logger = logging.getLogger(__name__)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        normalized_chapters = self._normalize_chapters(chapters, num_chapters, premise)
        world = self._build_world_scaffold(premise, genre, core, normalized_chapters)
        subworld_delta = self.plan_subworld_delta(
            premise=premise,
            genre=genre,
            arc_synopsis=str(core.get("arc_synopsis", "")).strip(),
            chapter_seed=normalized_chapters,
            existing_subworlds=[],
            focus_threads=[],
        )
        characters = self._merge_seed_characters(
            self._as_list(world.get("characters")),
            subworld_delta.get("new_subworlds"),
        )
        arc_outlines = self._derive_arc_outlines(
            overall_synopsis=str(core.get("arc_synopsis", "")).strip(),
            chapters=normalized_chapters,
        )

        result = {
            "arc_synopsis": str(core.get("arc_synopsis", "")).strip(),
            "setting_summary": str(core.get("setting_summary", "")).strip(),
            "initial_time": core.get("initial_time") or {
                "label": "现代雨季",
                "description": "故事开始于一场持续不断的雨夜。",
            },
            "chapters": normalized_chapters,
            "arc_outlines": arc_outlines,
            "characters": characters,
            "locations": self._as_list(world.get("locations")),
            "factions": self._as_list(world.get("factions")),
            "relations": self._as_list(world.get("relations")),
            "plot_threads": self._as_list(world.get("plot_threads")),
            "subworld_delta": subworld_delta,
        }
        logger.info("ArcDirector.plan_arc: parsed ok – keys=%s", list(result.keys()))
        return result

    def plan_subworld_delta(
        self,
        *,
        premise: str,
        genre: str,
        arc_synopsis: str,
        chapter_seed: list[dict[str, Any]],
        existing_subworlds: list[dict[str, Any] | Any],
        focus_threads: list[str] | None = None,
    ) -> dict:
        normalized_existing = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in (existing_subworlds or [])
            if isinstance(item, dict) or hasattr(item, "model_dump")
        ]
        normalized_focus_threads = [
            str(item).strip()
            for item in (focus_threads or [])
            if str(item).strip()
        ]
        fallback = self._fallback_subworld_delta(
            premise=premise,
            genre=genre,
            arc_synopsis=arc_synopsis,
            chapter_seed=chapter_seed,
            existing_subworlds=normalized_existing,
            focus_threads=normalized_focus_threads,
        )
        prompt = [
            {
                "role": "system",
                "content": "你是中文网文世界导演，只输出 JSON 对象，不要 markdown，不要解释。",
            },
            {
                "role": "user",
                "content": (
                    "请为当前 arc 规划 subworld delta，只返回 JSON。\n"
                    "顶层字段必须包含：reuse_subworld_ids、retire_subworld_ids、new_subworlds、initial_active_subworld_ids。\n"
                    "new_subworlds 每项必须包含：name、purpose、scope、chapter_window_hint、"
                    "core_named_characters、planned_slots、region_seeds。\n"
                    "scope 只能是 global_core 或 arc_local。\n"
                    "core_named_characters 每项必须包含：name、description、role_hint、importance。\n"
                    "planned_slots 每项必须包含：slot_key、role_hint、description。\n"
                    "region_seeds 每项必须包含：name、level、kind、parent_region_name、summary、"
                    "culture_traits、climate、terrain、controller_factions。\n"
                    "要求：\n"
                    "1. global_core 负责主角和长期常驻核心人物。\n"
                    "2. arc_local 负责当前 arc 会启用的局部圈层。\n"
                    "3. 每个 arc_local 最多 2 个 core_named_characters，最多 2 个 planned_slots。\n"
                    "4. 每个新 subworld 至少生成 1 个 level=1 的 region_seeds，可选 0-2 个 level=2，禁止 level=3。\n"
                    "5. level=2 必须通过 parent_region_name 指向同一 subworld 内的 level=1 地区。\n"
                    "6. 不要 retire global_core。\n"
                    "7. 如已有 subworld 可复用，则尽量复用。\n\n"
                    f"类型：{genre}\n"
                    f"故事前提：{premise}\n"
                    f"当前 arc 简述：{arc_synopsis}\n"
                    f"章节种子：{json.dumps(chapter_seed, ensure_ascii=False)}\n"
                    f"已有 subworld 摘要：{json.dumps(normalized_existing, ensure_ascii=False)}\n"
                    f"当前需要重点回收的线索：{json.dumps(normalized_focus_threads, ensure_ascii=False)}"
                ),
            },
        ]
        payload = self._call_json(
            prompt,
            temperature=0.35,
            max_tokens=min(self.max_tokens, 1300),
            fallback=fallback,
        )
        return self._normalize_subworld_delta(payload, fallback=fallback)

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
                    "priority": 1,
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

    def _fallback_subworld_delta(
        self,
        *,
        premise: str,
        genre: str,
        arc_synopsis: str,
        chapter_seed: list[dict[str, Any]],
        existing_subworlds: list[dict[str, Any]],
        focus_threads: list[str],
    ) -> dict:
        global_core_id = next(
            (
                str(item.get("id", "")).strip()
                for item in existing_subworlds
                if str(item.get("scope", "")).strip() == "global_core"
            ),
            "",
        )
        first_line = str((chapter_seed[0] or {}).get("one_line", "")).strip() if chapter_seed else ""
        focus_note = "、".join(focus_threads) if focus_threads else (first_line or premise[:16])
        protagonist_name = "顾临川"
        return {
            "reuse_subworld_ids": [global_core_id] if global_core_id else [],
            "retire_subworld_ids": [],
            "new_subworlds": [
                {
                    "name": "global_core",
                    "purpose": arc_synopsis[:80] or f"{genre}主角与长期常驻核心人物",
                    "scope": "global_core",
                    "chapter_window_hint": "opening",
                    "core_named_characters": [
                        {
                            "name": protagonist_name,
                            "description": "故事当前视角核心人物，负责承接主线推进与关键决断。",
                            "role_hint": "主角",
                            "importance": 10,
                            "aliases": [],
                            "initial_state": {},
                        }
                    ],
                    "planned_slots": [],
                    "region_seeds": [],
                },
                {
                    "name": "arc_local_1",
                    "purpose": focus_note or "当前 arc 的局部冲突舞台",
                    "scope": "arc_local",
                    "chapter_window_hint": "1-3",
                    "core_named_characters": [],
                    "planned_slots": [
                        {
                            "slot_key": "arc-local-contact",
                            "role_hint": "与当前 arc 冲突直接相关的本地联系人或关键人物",
                            "description": "进入当前阶段后最早与主角发生稳定互动的局部角色。",
                        }
                    ],
                    "region_seeds": [
                        {
                            "name": "局部核心区",
                            "level": 1,
                            "kind": "local_region",
                            "parent_region_name": "",
                            "summary": "当前 arc 的主要活动地区。",
                            "culture_traits": ["局部圈层自成规则"],
                            "climate": "气候特征与当前冲突相匹配。",
                            "terrain": ["复合地形"],
                            "controller_factions": ["局部主导势力"],
                        }
                    ],
                },
            ],
            "initial_active_subworld_ids": [global_core_id] if global_core_id else [],
        }

    def _normalize_subworld_delta(self, payload: dict, *, fallback: dict) -> dict:
        if not isinstance(payload, dict):
            return fallback
        normalized = {
            "reuse_subworld_ids": [
                str(item).strip()
                for item in (payload.get("reuse_subworld_ids") or [])
                if str(item).strip()
            ],
            "retire_subworld_ids": [
                str(item).strip()
                for item in (payload.get("retire_subworld_ids") or [])
                if str(item).strip()
                and str(item).strip() not in {"global_core", "global-core"}
            ],
            "new_subworlds": [],
            "initial_active_subworld_ids": [
                str(item).strip()
                for item in (payload.get("initial_active_subworld_ids") or [])
                if str(item).strip()
            ],
        }
        for raw in payload.get("new_subworlds") or []:
            if not isinstance(raw, dict):
                continue
            scope = str(raw.get("scope", "arc_local")).strip() or "arc_local"
            if scope not in {"global_core", "arc_local"}:
                scope = "arc_local"
            core_named_characters = []
            for seed in raw.get("core_named_characters") or []:
                if not isinstance(seed, dict):
                    continue
                name = str(seed.get("name", "")).strip()
                if not name:
                    continue
                core_named_characters.append(
                    {
                        "name": name,
                        "description": str(seed.get("description", "")).strip(),
                        "role_hint": str(seed.get("role_hint", "")).strip(),
                        "importance": max(1, _safe_int(seed.get("importance", 5) or 5, 5)),
                        "aliases": [
                            str(alias).strip()
                            for alias in (seed.get("aliases") or [])
                            if str(alias).strip()
                        ],
                        "initial_state": seed.get("initial_state") if isinstance(seed.get("initial_state"), dict) else {},
                    }
                )
            planned_slots = []
            for slot in raw.get("planned_slots") or []:
                if not isinstance(slot, dict):
                    continue
                slot_key = str(slot.get("slot_key", "")).strip() or f"slot-{len(planned_slots) + 1}"
                planned_slots.append(
                    {
                        "slot_key": slot_key,
                        "role_hint": str(slot.get("role_hint", "")).strip(),
                        "description": str(slot.get("description", "")).strip(),
                    }
                )
            region_seeds = []
            level_one_names: list[str] = []
            for seed in raw.get("region_seeds") or []:
                if not isinstance(seed, dict):
                    continue
                name = str(seed.get("name", "")).strip()
                if not name:
                    continue
                level = _safe_int(seed.get("level", 1) or 1, 1)
                if level not in {1, 2}:
                    level = 1
                parent_region_name = str(seed.get("parent_region_name", "")).strip()
                if level == 1:
                    parent_region_name = ""
                    level_one_names.append(name)
                elif parent_region_name not in level_one_names:
                    if level_one_names:
                        parent_region_name = level_one_names[0]
                    else:
                        level = 1
                        parent_region_name = ""
                        level_one_names.append(name)
                region_seeds.append(
                    {
                        "name": name,
                        "level": level,
                        "kind": str(seed.get("kind", "")).strip() or "local_region",
                        "parent_region_name": parent_region_name,
                        "summary": str(seed.get("summary", "")).strip(),
                        "culture_traits": [
                            str(item).strip()
                            for item in (seed.get("culture_traits") or [])
                            if str(item).strip()
                        ],
                        "climate": str(seed.get("climate", "")).strip(),
                        "terrain": [
                            str(item).strip()
                            for item in (seed.get("terrain") or [])
                            if str(item).strip()
                        ],
                        "controller_factions": [
                            str(item).strip()
                            for item in (seed.get("controller_factions") or [])
                            if str(item).strip()
                        ],
                    }
                )
            if scope == "arc_local" and not region_seeds:
                region_seeds = [
                    {
                        "name": "局部核心区",
                        "level": 1,
                        "kind": "local_region",
                        "parent_region_name": "",
                        "summary": "当前 arc 的主要活动地区。",
                        "culture_traits": ["局部圈层自成规则"],
                        "climate": "气候特征与当前冲突相匹配。",
                        "terrain": ["复合地形"],
                        "controller_factions": ["局部主导势力"],
                    }
                ]
            normalized["new_subworlds"].append(
                {
                    "subworld_id": str(raw.get("subworld_id", "")).strip(),
                    "parent_subworld_id": str(raw.get("parent_subworld_id", "")).strip(),
                    "name": str(raw.get("name", "")).strip() or f"subworld-{len(normalized['new_subworlds']) + 1}",
                    "purpose": str(raw.get("purpose", "")).strip(),
                    "scope": scope,
                    "chapter_window_hint": str(raw.get("chapter_window_hint", "")).strip(),
                    "core_named_characters": core_named_characters,
                    "planned_slots": planned_slots,
                    "region_seeds": region_seeds,
                }
            )
        if not normalized["new_subworlds"]:
            return fallback
        return normalized

    def _merge_seed_characters(
        self,
        world_characters: list[dict],
        new_subworlds: Any,
    ) -> list[dict]:
        merged: list[dict] = []
        seen_names: set[str] = set()
        for item in world_characters:
            name = str(item.get("name", "")).strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            merged.append(item)
        for subworld in self._as_list(new_subworlds):
            for seed in subworld.get("core_named_characters") or []:
                if not isinstance(seed, dict):
                    continue
                name = str(seed.get("name", "")).strip()
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                merged.append(
                    {
                        "name": name,
                        "description": str(seed.get("description", "")).strip(),
                        "aliases": [
                            str(alias).strip()
                            for alias in (seed.get("aliases") or [])
                            if str(alias).strip()
                        ],
                        "importance": max(1, _safe_int(seed.get("importance", 5) or 5, 5)),
                        "initial_state": seed.get("initial_state") if isinstance(seed.get("initial_state"), dict) else {},
                    }
                )
        return merged

    def _derive_arc_outlines(
        self,
        *,
        overall_synopsis: str,
        chapters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not chapters:
            return []
        sizes = allocate_arc_chapter_sizes(len(chapters))
        outlines: list[dict[str, Any]] = []
        cursor = 0
        for index, size in enumerate(sizes, start=1):
            chapter_slice = chapters[cursor : cursor + size]
            if not chapter_slice:
                break
            chapter_start = int(chapter_slice[0].get("chapter_number", cursor + 1) or (cursor + 1))
            chapter_end = int(chapter_slice[-1].get("chapter_number", chapter_start + len(chapter_slice) - 1) or (chapter_start + len(chapter_slice) - 1))
            synopsis = self._summarize_arc_slice(
                overall_synopsis=overall_synopsis,
                chapter_slice=chapter_slice,
                arc_index=index,
                total_arcs=len(sizes),
            )
            outlines.append(
                {
                    "arc_number": index,
                    "chapter_start": chapter_start,
                    "chapter_end": chapter_end,
                    "chapter_count": len(chapter_slice),
                    "arc_synopsis": synopsis,
                }
            )
            cursor += size
        return outlines or [
            {
                "arc_number": 1,
                "chapter_start": 1,
                "chapter_end": len(chapters),
                "chapter_count": len(chapters),
                "arc_synopsis": overall_synopsis,
            }
        ]

    @staticmethod
    def _summarize_arc_slice(
        *,
        overall_synopsis: str,
        chapter_slice: list[dict[str, Any]],
        arc_index: int,
        total_arcs: int,
    ) -> str:
        if total_arcs <= 1:
            return overall_synopsis
        one_lines = [
            str(item.get("one_line", "")).strip()
            for item in chapter_slice[:3]
            if str(item.get("one_line", "")).strip()
        ]
        title = "；".join(one_lines[:2]) if one_lines else ""
        prefix = f"第{arc_index}弧"
        if arc_index == 1:
            prefix = "开篇弧"
        elif arc_index == total_arcs:
            prefix = "收束弧"
        if title:
            return f"{prefix}：{title}"
        return f"{prefix}：{overall_synopsis}"

    def draft_arc_structure(
        self,
        *,
        premise: str,
        genre: str,
        total_chapters: int,
        policy_tier: str,
        base_target_size: int,
        chapter_seed: list[dict[str, Any]],
        audience_trends: list[str] | None = None,
    ) -> dict:
        normalized_trends = [str(item).strip() for item in (audience_trends or []) if str(item).strip()]
        fallback = {
            "phase_layout": ["setup", "pressure", "turn", "payoff"],
            "key_beats": [
                item.get("one_line") or item.get("title") or f"第{index + 1}章推进"
                for index, item in enumerate(chapter_seed[:4])
            ],
            "thread_priorities": [
                {
                    "name": f"{genre}主线",
                    "priority": 1,
                    "reason": "当前 active arc 的核心冲突线",
                }
            ],
            "hotspot_candidates": [
                item.get("title") or item.get("one_line") or f"第{index + 1}章热点"
                for index, item in enumerate(chapter_seed[:3])
            ],
            "compression_candidates": [
                item.get("title") or item.get("one_line") or f"第{index + 1}章压缩候选"
                for index, item in enumerate(chapter_seed[2:4])
            ],
            "reader_promise": {
                "genre_promise": f"{genre}网文",
                "pleasure_promise": f"{genre}读者期待稳定获得爽点与悬念回报",
                "core_pleasures": ["稳定回报", "悬念升级", "高压翻盘"],
                "acceptable_drag_level": "low",
                "acceptable_exposition_density": "medium",
                "cliffhanger_aggressiveness": "high",
                "ambiguity_mode": "managed",
                "world_legibility_target": "规则需要足够清晰，让关键反转显得合理而非强行。",
            },
            "arc_payoff_map": {
                "macro_payoffs": [
                    {
                        "payoff_id": "payoff-1",
                        "category": "mystery",
                        "template_id": "mystery-locked-clue",
                        "target_chapter_hint": "arc-mid",
                        "setup_requirement": "前期埋下异象与错误认知",
                        "success_signal": "读者感到真相逼近但未完全揭晓",
                    },
                    {
                        "payoff_id": "payoff-2",
                        "category": "power",
                        "template_id": "power-hidden-edge",
                        "target_chapter_hint": "arc-late",
                        "setup_requirement": "主角先承受压制",
                        "success_signal": "主角在关键节点翻盘",
                    },
                ],
                "awe_kit": ["失控异象", "身份反转", "代价换胜"],
                "revelation_layers": [
                    {
                        "layer_id": "rule-layer-1",
                        "layer_type": "rule",
                        "summary": "揭开一条可被理解的世界规则，并明确其限制。",
                        "chapter_window": "arc-mid",
                    },
                    {
                        "layer_id": "faction-layer-1",
                        "layer_type": "faction",
                        "summary": "暴露一个隐藏势力对当前冲突的真实意图。",
                        "chapter_window": "arc-late",
                    },
                ],
                "ambiguity_constraints": [
                    "超常现象可以误导认知，但不能无代价地改写既有因果。",
                    "关键翻盘必须能回指前文线索或规则。",
                ],
            },
        }
        trend_text = " ".join(normalized_trends)
        if "character_heat" in trend_text or "relationship_interest" in trend_text:
            fallback["reader_promise"]["core_pleasures"].append("角色关系与地位波动")
            fallback["arc_payoff_map"]["macro_payoffs"].append(
                {
                    "payoff_id": "payoff-3",
                    "category": "emotion",
                    "template_id": "emotion-knife-turn",
                    "target_chapter_hint": "arc-late",
                    "setup_requirement": "让关键角色先建立情感或立场连结",
                    "success_signal": "角色关系发生明确变化并增强追读意图",
                }
            )
        if "confusion" in trend_text or "risk" in trend_text or "prediction" in trend_text:
            fallback["reader_promise"]["world_legibility_target"] = "每个关键反转都要让读者看得懂代价、边界与因果。"
            fallback["arc_payoff_map"]["ambiguity_constraints"].append("所有认知反转都必须回指前文线索。")
        if "pacing" in trend_text:
            fallback["reader_promise"]["acceptable_drag_level"] = "low"
            fallback["reader_promise"]["cliffhanger_aggressiveness"] = "high"
        prompt = [
            {
                "role": "system",
                "content": "你是网文 arc 结构导演，只输出 JSON 对象。",
            },
            {
                "role": "user",
                "content": (
                    "请为当前 active arc 生成中层结构草案，只返回 JSON。\n"
                    "字段必须包含：phase_layout、key_beats、thread_priorities、"
                    "hotspot_candidates、compression_candidates、reader_promise、arc_payoff_map。\n"
                    "thread_priorities 中每项必须有 name/priority/reason。\n\n"
                    "reader_promise 必须包含：genre_promise、pleasure_promise、core_pleasures、"
                    "acceptable_drag_level、acceptable_exposition_density、cliffhanger_aggressiveness、"
                    "ambiguity_mode、world_legibility_target。\n"
                    "arc_payoff_map 必须包含：macro_payoffs、awe_kit、revelation_layers、ambiguity_constraints；"
                    "macro_payoffs 中每项必须有 payoff_id/category/template_id/"
                    "target_chapter_hint/setup_requirement/success_signal。\n\n"
                    "revelation_layers 中每项必须有 layer_id、layer_type、summary、chapter_window。\n\n"
                    f"类型：{genre}\n"
                    f"全书目标章节数：{total_chapters}\n"
                    f"当前 policy tier：{policy_tier}\n"
                    f"当前 arc 的基础 target：{base_target_size}\n"
                    f"故事前提：{premise}\n"
                    f"读者长窗趋势：{json.dumps(normalized_trends, ensure_ascii=False)}\n"
                    f"近端章节种子：{json.dumps(chapter_seed, ensure_ascii=False)}"
                ),
            },
        ]
        return self._call_json(
            prompt,
            temperature=0.4,
            max_tokens=min(self.max_tokens, 1000),
            fallback=fallback,
        )

    def analyze_arc_envelope(
        self,
        *,
        total_chapters: int,
        policy_tier: str,
        base_target_size: int,
        base_soft_min: int,
        base_soft_max: int,
        structure_draft: dict[str, Any],
        provisional_band: list[dict[str, Any]],
    ) -> dict:
        fallback = {
            "recommendation": "keep",
            "evidence": [
                f"policy_tier={policy_tier}",
                f"base_target={base_target_size}",
                f"provisional_band={len(provisional_band)}",
            ],
            "expansion_signals": [],
            "compression_signals": [],
            "suggested_target": base_target_size,
            "suggested_soft_min": base_soft_min,
            "suggested_soft_max": base_soft_max,
            "confidence": 0.65,
        }
        prompt = [
            {
                "role": "system",
                "content": "你是网文 arc envelope 分析器，只输出 JSON 对象。",
            },
            {
                "role": "user",
                "content": (
                    "请根据 current active arc 的结构草案和 provisional band，"
                    "输出 keep / expand / compress 建议。\n"
                    "字段必须包含：recommendation、evidence、expansion_signals、"
                    "compression_signals、suggested_target、suggested_soft_min、"
                    "suggested_soft_max、confidence。\n\n"
                    f"全书目标章节数：{total_chapters}\n"
                    f"policy tier：{policy_tier}\n"
                    f"base target：{base_target_size}\n"
                    f"base soft range：{base_soft_min} ~ {base_soft_max}\n"
                    f"ArcStructureDraft：{json.dumps(structure_draft, ensure_ascii=False)}\n"
                    f"Provisional band：{json.dumps(provisional_band, ensure_ascii=False)}"
                ),
            },
        ]
        return self._call_json(
            prompt,
            temperature=0.35,
            max_tokens=min(self.max_tokens, 1100),
            fallback=fallback,
        )

    def rehearse_scenario(self, *, report: dict[str, Any]) -> dict:
        fallback = {
            "recommendation": "pass",
            "risk_findings": [],
            "future_conflicts": [],
            "required_plan_patches": [],
        }
        prompt = [
            {
                "role": "system",
                "content": "你是叙事 preflight 导演，只输出 JSON 对象，不要 markdown。",
            },
            {
                "role": "user",
                "content": (
                    "请审查 Scenario Rehearsal 报告，重点判断误会/欺骗是否成立、"
                    "读者认知是否过早或过晚、当前计划是否锁死未来 arc。\n"
                    "只输出字段：recommendation(pass|patch|replan|block)、risk_findings、"
                    "future_conflicts、required_plan_patches。\n\n"
                    f"ScenarioRehearsalReport={json.dumps(report, ensure_ascii=False)}"
                ),
            },
        ]
        return self._call_json(
            prompt,
            temperature=0.2,
            max_tokens=min(self.max_tokens, 1200),
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
        if hasattr(self.llm_client, "api_key") and not getattr(self.llm_client, "api_key", "").strip():
            return fallback
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
                if isinstance(exc, LLMJSONParseError) and exc.empty_response:
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
