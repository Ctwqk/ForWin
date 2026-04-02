from __future__ import annotations

import json
import logging

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneOutput, ScenePlan
from forwin.protocol.state_change import (
    EventCandidate,
    StateChangeCandidate,
    ThreadBeatCandidate,
    TimeAdvance,
)
from forwin.protocol.writer import WriterOutput
from .llm_client import LLMClient
from .prompts import (
    build_single_chapter_draft_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
    build_structured_extraction_prompt,
)
from forwin.utils import parse_llm_json

logger = logging.getLogger(__name__)


class ChapterWriter:
    """Generates chapters using LLM.

    Usage::

        client = LLMClient(api_key="...")
        writer = ChapterWriter(client)
        arc    = writer.plan_arc(premise, genre, num_chapters=3)
        output = writer.write_chapter(context_pack)
    """

    def __init__(
        self,
        llm_client: LLMClient,
        temperature: float = 0.85,
        max_tokens: int = 16384,
        writer_mode: str = "scene",
        default_scene_count: int = 3,
        max_scene_count: int = 4,
        min_chapter_chars: int = 1500,
        max_chapter_chars: int = 2200,
        target_chapter_chars: int = 2000,
        single_call_timeout_seconds: float = 90.0,
        scene_call_timeout_seconds: float = 45.0,
    ) -> None:
        self.llm_client = llm_client
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.writer_mode = writer_mode
        self.default_scene_count = default_scene_count
        self.max_scene_count = max_scene_count
        self.min_chapter_chars = min_chapter_chars
        self.max_chapter_chars = max_chapter_chars
        self.target_chapter_chars = max(
            self.min_chapter_chars,
            min(int(target_chapter_chars), self.max_chapter_chars),
        )
        self.single_call_timeout_seconds = max(10.0, float(single_call_timeout_seconds))
        self.scene_call_timeout_seconds = max(10.0, float(scene_call_timeout_seconds))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_chapter(self, context: ChapterContextPack) -> WriterOutput:
        """Write a single chapter.

        Returns a fully-populated WriterOutput.  The char_count field is
        computed from the body after parsing.
        """
        if self.writer_mode == "single":
            return self._write_single_chapter(context)
        return self._write_scene_chapter(context)

    def _write_single_chapter(
        self,
        context: ChapterContextPack,
        *,
        timeout_seconds: float | None = None,
        max_attempts: int = 2,
        retry_on_timeout: bool = True,
    ) -> WriterOutput:
        logger.info(
            "write_chapter(single): chapter=%d title_plan=%r",
            context.chapter_number,
            context.chapter_plan_title,
        )
        target_chars = max(
            self.min_chapter_chars,
            min(self.target_chapter_chars, self.max_chapter_chars),
        )
        max_output_tokens = min(
            self.max_tokens,
            max(2600, int(target_chars * 1.8)),
        )
        draft_data = self._chat_json(
            build_single_chapter_draft_prompt(
                context,
                target_chars=target_chars,
                min_chars=self.min_chapter_chars,
                max_chars=self.max_chapter_chars,
            ),
            temperature=self.temperature,
            max_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds or self.single_call_timeout_seconds,
            max_attempts=max_attempts,
            retry_on_timeout=retry_on_timeout,
        )
        extracted = self._extract_structured(
            context,
            draft_data.get("title", context.chapter_plan_title or f"第{context.chapter_number}章"),
            draft_data.get("body", ""),
        )
        merged = dict(draft_data)
        merged.update(extracted)
        output = self._writer_output_from_dict(context, merged)
        output.generation_meta.update(
            {"mode": "single", "call_count": 2, "scene_count": len(output.scene_outputs)}
        )
        return output

    def _write_scene_chapter(self, context: ChapterContextPack) -> WriterOutput:
        logger.info(
            "write_chapter(scene): chapter=%d title_plan=%r",
            context.chapter_number,
            context.chapter_plan_title,
        )
        try:
            scene_plans = self._plan_scenes(context)
            scene_outputs = [
                self._generate_scene(context, scene_plan) for scene_plan in scene_plans
            ]
            stitched = self._stitch_scenes(context, scene_outputs)
            extracted = self._extract_structured(
                context,
                stitched.get("title", context.chapter_plan_title or f"第{context.chapter_number}章"),
                stitched.get("body", ""),
            )

            merged = dict(stitched)
            merged.update(extracted)
            output = self._writer_output_from_dict(context, merged, scene_outputs=scene_outputs)
            output.generation_meta.update(
                {
                    "mode": "scene",
                    "scene_count": len(scene_outputs),
                    "call_count": len(scene_outputs) + 3,
                }
            )
            return output
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scene chapter generation failed, falling back to single draft mode: %s",
                exc,
            )
            output = self._write_single_chapter(
                context,
                timeout_seconds=self.scene_call_timeout_seconds,
                max_attempts=1,
                retry_on_timeout=False,
            )
            output.generation_meta.update(
                {
                    "fallback_from_scene": True,
                    "scene_fallback_error": str(exc),
                }
            )
            return output

    def _writer_output_from_dict(
        self,
        context: ChapterContextPack,
        data: dict,
        scene_outputs: list[SceneOutput] | None = None,
    ) -> WriterOutput:
        state_changes = self._build_list(data, "state_changes", StateChangeCandidate)
        new_events = self._build_list(data, "new_events", EventCandidate)
        thread_beats = self._build_list(data, "thread_beats", ThreadBeatCandidate)
        time_advance: TimeAdvance | None = None
        if data.get("time_advance"):
            try:
                time_advance = TimeAdvance(**data["time_advance"])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "write_chapter: could not parse time_advance (%s) – skipping: %s",
                    data["time_advance"],
                    exc,
                )

        body: str = data.get("body", "")
        output = WriterOutput(
            project_id=getattr(context, "project_id", ""),
            chapter_number=context.chapter_number,
            title=data.get("title", f"第{context.chapter_number}章"),
            body=body,
            char_count=len(body),
            end_of_chapter_summary=data.get("end_of_chapter_summary", ""),
            scene_outputs=scene_outputs or [],
            state_changes=state_changes,
            new_events=new_events,
            thread_beats=thread_beats,
            time_advance=time_advance,
            generation_meta=dict(data.get("_generation_meta") or {}),
        )
        logger.info(
            "write_chapter: done – chapter=%d char_count=%d "
            "state_changes=%d new_events=%d thread_beats=%d",
            output.chapter_number,
            output.char_count,
            len(output.state_changes),
            len(output.new_events),
            len(output.thread_beats),
        )
        return output

    def _plan_scenes(self, context: ChapterContextPack) -> list[ScenePlan]:
        try:
            data = self._chat_json(
                build_scene_breakdown_prompt(
                    context,
                    default_scene_count=self.default_scene_count,
                    max_scene_count=self.max_scene_count,
                ),
                temperature=0.6,
                max_tokens=min(
                    self.max_tokens,
                    max(1200, self.default_scene_count * 420),
                ),
                timeout_seconds=self.scene_call_timeout_seconds,
                max_attempts=1,
                retry_on_timeout=False,
            )
            scenes = self._build_list(data, "scenes", ScenePlan)
            if scenes:
                return scenes[: self.max_scene_count]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scene breakdown failed, falling back to heuristic scenes: %s",
                exc,
            )

        fallback_count = min(max(self.default_scene_count, 2), self.max_scene_count)
        goals = context.chapter_goals or [context.chapter_plan_one_line or "推进本章主线"]
        base_target = max(
            700,
            min(self.target_chapter_chars, self.max_chapter_chars) // fallback_count,
        )
        return [
            ScenePlan(
                scene_no=index + 1,
                objective=goals[min(index, len(goals) - 1)],
                must_progress_points=[goals[min(index, len(goals) - 1)]],
                target_chars=base_target,
            )
            for index in range(fallback_count)
        ]

    def _generate_scene(self, context: ChapterContextPack, scene_plan: ScenePlan) -> SceneOutput:
        max_output_tokens = min(
            self.max_tokens,
            max(1400, int(max(scene_plan.target_chars, 600) * 1.8)),
        )
        data = self._chat_json(
            build_scene_generation_prompt(context, scene_plan),
            temperature=self.temperature,
            max_tokens=max_output_tokens,
            timeout_seconds=self.scene_call_timeout_seconds,
            max_attempts=2,
            retry_on_timeout=False,
        )
        return SceneOutput(
            scene_no=scene_plan.scene_no,
            scene_objective=scene_plan.objective,
            scene_time_point=data.get("scene_time_point", scene_plan.time_hint),
            scene_location_id=data.get("scene_location_id", scene_plan.location_hint),
            involved_entities=data.get("involved_entities", scene_plan.involved_entities),
            text=data.get("text", ""),
            micro_summary=data.get("micro_summary", ""),
        )

    def _stitch_scenes(
        self,
        context: ChapterContextPack,
        scene_outputs: list[SceneOutput],
    ) -> dict:
        return self._chat_json(
            build_scene_stitch_prompt(context, scene_outputs),
            temperature=0.5,
            max_tokens=min(self.max_tokens, 2400),
            timeout_seconds=self.scene_call_timeout_seconds,
            max_attempts=2,
            retry_on_timeout=False,
        )

    def _extract_structured(
        self,
        context: ChapterContextPack,
        chapter_title: str,
        chapter_body: str,
    ) -> dict:
        prompt = build_structured_extraction_prompt(context, chapter_title, chapter_body)
        try:
            return self._chat_json(
                prompt,
                temperature=0.3,
                max_tokens=min(self.max_tokens, 1400),
                timeout_seconds=self.scene_call_timeout_seconds,
                max_attempts=2,
                retry_on_timeout=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "structured extraction primary pass failed, retrying with reduced body: %s",
                exc,
            )
            shortened_body = chapter_body[: max(800, min(len(chapter_body), 1800))]
            try:
                return self._chat_json(
                    build_structured_extraction_prompt(
                        context,
                        chapter_title,
                        shortened_body,
                    ),
                    temperature=0.2,
                    max_tokens=min(self.max_tokens, 900),
                    timeout_seconds=self.scene_call_timeout_seconds,
                    max_attempts=1,
                    retry_on_timeout=False,
                )
            except Exception as repair_exc:  # noqa: BLE001
                logger.warning(
                    "structured extraction degraded to empty metadata after retry: %s",
                    repair_exc,
                )
                return {
                    "new_events": [],
                    "state_changes": [],
                    "thread_beats": [],
                    "time_advance": None,
                    "_generation_meta": {
                        "structured_extraction": "degraded",
                        "structured_extraction_error": str(repair_exc),
                    },
                }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_list(data: dict, key: str, model_cls) -> list:  # type: ignore[type-arg]
        """Build a list of pydantic model instances from a raw list in *data*.

        Silently skips items that fail to parse, logging a warning for each.
        """
        raw_list = data.get(key)
        if not isinstance(raw_list, list):
            return []
        result = []
        for i, item in enumerate(raw_list):
            if not isinstance(item, dict):
                logger.warning(
                    "_build_list: %s[%d] is not a dict, skipping: %r", key, i, item
                )
                continue
            try:
                result.append(model_cls(**item))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_build_list: %s[%d] failed to parse (%s) – skipping: %r",
                    key,
                    i,
                    exc,
                    item,
                )
        return result

    def _chat_json(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float | None = None,
        max_attempts: int = 3,
        retry_on_timeout: bool = True,
    ) -> dict:
        attempts = [
            {"temperature": temperature, "max_tokens": max_tokens},
            {
                "temperature": max(0.2, temperature - 0.15),
                "max_tokens": max(420, min(max_tokens, 1400)),
            },
            {"temperature": 0.2, "max_tokens": max(360, min(max_tokens, 900))},
        ][: max(1, int(max_attempts))]
        last_error: Exception | None = None
        for index, attempt in enumerate(attempts, start=1):
            try:
                raw = self._chat_with_compat(
                    messages,
                    temperature=attempt["temperature"],
                    max_tokens=attempt["max_tokens"],
                    timeout_seconds=timeout_seconds,
                    retry_on_timeout=retry_on_timeout,
                )
                return parse_llm_json(raw, error_prefix="ChapterWriter JSON parser")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "ChapterWriter JSON call failed on attempt %d/%d: %s",
                    index,
                    len(attempts),
                    exc,
                )
        raise ValueError(f"ChapterWriter JSON generation failed after retries: {last_error}")

    def _chat_with_compat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float | None,
        retry_on_timeout: bool,
    ) -> str:
        variants = [
            {
                "response_format": {"type": "json_object"},
                "timeout_seconds": timeout_seconds,
                "retry_on_timeout": retry_on_timeout,
            },
            {
                "response_format": {"type": "json_object"},
            },
            {
                "timeout_seconds": timeout_seconds,
                "retry_on_timeout": retry_on_timeout,
            },
            {},
        ]
        last_type_error: TypeError | None = None
        for extra in variants:
            try:
                return self.llm_client.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **extra,
                )
            except TypeError as exc:
                message = str(exc)
                if not any(
                    name in message
                    for name in ("response_format", "timeout_seconds", "retry_on_timeout")
                ):
                    raise
                last_type_error = exc
                continue
        raise last_type_error or RuntimeError("chat compatibility fallback failed")
