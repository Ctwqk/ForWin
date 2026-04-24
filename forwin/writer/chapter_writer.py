from __future__ import annotations

import json
import inspect
import logging
import re

from forwin.model_adapter import ModelAdapter
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneContinuation, SceneOutput, ScenePlan
from forwin.protocol.state_change import (
    EventCandidate,
    StateChangeCandidate,
    ThreadBeatCandidate,
    TimeAdvance,
)
from forwin.protocol.writer import EntityMention, LoreCandidate, TimelineHint, WriterNote, WriterOutput
from forwin.skills import serialize_prompt_layers
from .prompts import (
    build_preview_chapter_prompt,
    build_lore_timeline_notes_extraction_prompt,
    build_state_event_extraction_prompt,
    build_single_chapter_draft_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
    build_thread_time_extraction_prompt,
)
from forwin.utils import parse_llm_json

logger = logging.getLogger(__name__)
_VALID_REWARD_TAGS = {"power", "social", "justice", "mystery", "emotion"}


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
        llm_client: ModelAdapter,
        temperature: float = 0.85,
        max_tokens: int = 16384,
        writer_mode: str = "scene",
        default_scene_count: int = 3,
        max_scene_count: int = 4,
        min_chapter_chars: int = 2500,
        max_chapter_chars: int = 3200,
        target_chapter_chars: int = 2800,
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
        self._chat_signature = inspect.signature(self.llm_client.chat)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_chapter(
        self,
        context: ChapterContextPack,
        *,
        skill_layers: list[object] | None = None,
        trace_stage_key: str = "chapter_draft",
    ) -> WriterOutput:
        """Write a single chapter.

        Returns a fully-populated WriterOutput.  The char_count field is
        computed from the body after parsing.
        """
        if self.writer_mode == "single":
            return self._write_single_chapter(
                context,
                skill_layers=skill_layers,
                trace_stage_key=trace_stage_key,
            )
        return self._write_scene_chapter(
            context,
            skill_layers=skill_layers,
            trace_stage_key=trace_stage_key,
        )

    def write_preview_chapter(
        self,
        context: ChapterContextPack,
        *,
        skill_layers: list[object] | None = None,
        trace_stage_key: str = "chapter_draft",
        timeout_seconds: float | None = None,
        max_attempts: int = 2,
        retry_on_timeout: bool = True,
    ) -> WriterOutput:
        """Write a lightweight provisional preview chapter.

        Preview generation prioritizes producing a readable draft with a
        single LLM call. It intentionally skips structured extraction so
        provisional execution can retain real preview text without paying the
        latency and failure cost of a second LLM pass.
        """
        logger.info(
            "write_chapter(preview): chapter=%d title_plan=%r",
            context.chapter_number,
            context.chapter_plan_title,
        )
        target_chars = max(
            self.min_chapter_chars,
            min(self.target_chapter_chars, self.max_chapter_chars),
        )
        max_output_tokens = min(
            self.max_tokens,
            max(1800, int(target_chars * 1.8)),
        )
        base_messages = build_preview_chapter_prompt(
            context,
            target_chars=target_chars,
            min_chars=self.min_chapter_chars,
            max_chars=self.max_chapter_chars,
        )
        preview_text = self._chat_preview_text(
            build_preview_chapter_prompt(
                context,
                target_chars=target_chars,
                min_chars=self.min_chapter_chars,
                max_chars=self.max_chapter_chars,
                skill_layers=skill_layers,
            ),
            temperature=min(self.temperature, 0.7),
            max_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds or self.single_call_timeout_seconds,
            max_attempts=max_attempts,
            retry_on_timeout=retry_on_timeout,
        )
        draft_data = self._parse_preview_text(
            preview_text,
            fallback_title=context.chapter_plan_title or f"第{context.chapter_number}章",
        )
        title = draft_data.get(
            "title",
            context.chapter_plan_title or f"第{context.chapter_number}章",
        )
        body = str(draft_data.get("body", "") or "")
        output = WriterOutput(
            project_id=getattr(context, "project_id", ""),
            chapter_number=context.chapter_number,
            title=title,
            body=body,
            char_count=len(body),
            end_of_chapter_summary=self._preview_summary(
                draft_data.get("end_of_chapter_summary"),
                title,
                body,
            ),
            generation_meta={
                "mode": "provisional_preview",
                "call_count": 1,
                "structured_extraction": "skipped",
                "prompt_trace": self._build_prompt_trace(
                    base_messages=base_messages,
                    skill_layers=skill_layers,
                    template_id="writer:preview",
                    stage_key=trace_stage_key,
                    input_snapshot={
                        "writer_mode": "preview",
                        "chapter_number": context.chapter_number,
                    },
                    output_summary={
                        "mode": "provisional_preview",
                        "title": title,
                        "char_count": len(body),
                    },
                ),
            },
        )
        self._attach_llm_fallback_events(output)
        logger.info(
            "write_preview_chapter: done – chapter=%d char_count=%d",
            output.chapter_number,
            output.char_count,
        )
        return output

    def _write_single_chapter(
        self,
        context: ChapterContextPack,
        *,
        skill_layers: list[object] | None = None,
        trace_stage_key: str = "chapter_draft",
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
        base_messages = build_single_chapter_draft_prompt(
            context,
            target_chars=target_chars,
            min_chars=self.min_chapter_chars,
            max_chars=self.max_chapter_chars,
        )
        raw_draft = self._chat_preview_text(
            build_single_chapter_draft_prompt(
                context,
                target_chars=target_chars,
                min_chars=self.min_chapter_chars,
                max_chars=self.max_chapter_chars,
                skill_layers=skill_layers,
            ),
            temperature=self.temperature,
            max_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds or self.single_call_timeout_seconds,
            max_attempts=max_attempts,
            retry_on_timeout=retry_on_timeout,
        )
        draft_data = self._parse_preview_text(
            raw_draft,
            fallback_title=context.chapter_plan_title or f"第{context.chapter_number}章",
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
            {
                "mode": "single",
                "call_count": 4,
                "scene_count": len(output.scene_outputs),
                "prompt_trace": self._build_prompt_trace(
                    base_messages=base_messages,
                    skill_layers=skill_layers,
                    template_id="writer:single",
                    stage_key=trace_stage_key,
                    input_snapshot={
                        "writer_mode": "single",
                        "chapter_number": context.chapter_number,
                    },
                    output_summary={
                        "mode": "single",
                        "title": output.title,
                        "char_count": output.char_count,
                    },
                ),
            }
        )
        return output

    def _write_scene_chapter(
        self,
        context: ChapterContextPack,
        *,
        skill_layers: list[object] | None = None,
        trace_stage_key: str = "chapter_draft",
    ) -> WriterOutput:
        logger.info(
            "write_chapter(scene): chapter=%d title_plan=%r",
            context.chapter_number,
            context.chapter_plan_title,
        )
        try:
            base_messages = build_scene_breakdown_prompt(
                context,
                default_scene_count=self.default_scene_count,
                max_scene_count=self.max_scene_count,
            )
            scene_plans = self._plan_scenes(context, skill_layers=skill_layers)
            scene_outputs = [
                self._generate_scene(context, scene_plan, skill_layers=skill_layers)
                for scene_plan in scene_plans
            ]
            stitched = self._stitch_scenes(context, scene_outputs, skill_layers=skill_layers)
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
                    "call_count": len(scene_outputs) + 5,
                    "prompt_trace": self._build_prompt_trace(
                        base_messages=base_messages,
                        skill_layers=skill_layers,
                        template_id="writer:scene_pipeline",
                        stage_key=trace_stage_key,
                        input_snapshot={
                            "writer_mode": "scene",
                            "chapter_number": context.chapter_number,
                            "target_scene_count": self.default_scene_count,
                        },
                        output_summary={
                            "mode": "scene",
                            "title": output.title,
                            "char_count": output.char_count,
                            "scene_count": len(scene_outputs),
                        },
                    ),
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
                skill_layers=skill_layers,
                trace_stage_key=trace_stage_key,
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
        lore_candidates = self._build_list(data, "lore_candidates", LoreCandidate)
        timeline_hints = self._build_list(data, "timeline_hints", TimelineHint)
        writer_notes = self._build_list(data, "writer_notes", WriterNote)
        entity_mentions = self._build_list(data, "entity_mentions", EntityMention)
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
        resolved_scene_outputs = scene_outputs or []
        output = WriterOutput(
            project_id=getattr(context, "project_id", ""),
            chapter_number=context.chapter_number,
            title=data.get("title", f"第{context.chapter_number}章"),
            body=body,
            char_count=len(body),
            end_of_chapter_summary=data.get("end_of_chapter_summary", ""),
            scene_outputs=resolved_scene_outputs,
            state_changes=state_changes,
            new_events=new_events,
            thread_beats=thread_beats,
            time_advance=time_advance,
            scene_continuation=[
                scene.continuation
                for scene in resolved_scene_outputs
                if scene.continuation.scene_no or scene.continuation.continuity_anchor
            ],
            lore_candidates=lore_candidates,
            timeline_hints=timeline_hints,
            writer_notes=writer_notes,
            entity_mentions=entity_mentions,
            generation_meta=dict(data.get("_generation_meta") or {}),
        )
        self._attach_llm_fallback_events(output)
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

    def _plan_scenes(
        self,
        context: ChapterContextPack,
        *,
        skill_layers: list[object] | None = None,
    ) -> list[ScenePlan]:
        try:
            data = self._chat_json(
                build_scene_breakdown_prompt(
                    context,
                    default_scene_count=self.default_scene_count,
                    max_scene_count=self.max_scene_count,
                    skill_layers=skill_layers,
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
                reward_beat_tag=self._fallback_reward_tag(context, index),
                immersion_anchor=self._fallback_immersion_anchor(context, index),
                progress_marker=self._fallback_progress_marker(context, index),
                target_chars=base_target,
            )
            for index in range(fallback_count)
        ]

    def _generate_scene(
        self,
        context: ChapterContextPack,
        scene_plan: ScenePlan,
        *,
        skill_layers: list[object] | None = None,
    ) -> SceneOutput:
        max_output_tokens = min(
            self.max_tokens,
            max(1400, int(max(scene_plan.target_chars, 600) * 1.8)),
        )
        raw_scene = self._chat_preview_text(
            build_scene_generation_prompt(context, scene_plan, skill_layers=skill_layers),
            temperature=self.temperature,
            max_tokens=max_output_tokens,
            timeout_seconds=self.scene_call_timeout_seconds,
            max_attempts=2,
            retry_on_timeout=False,
        )
        data = self._parse_tagged_text(
            raw_scene,
            fallback_title="",
            extra_markers={
                "<<FORWIN_TIME>>": "scene_time_point",
                "<<FORWIN_LOCATION>>": "scene_location_id",
                "<<FORWIN_ENTITIES>>": "involved_entities",
                "<<FORWIN_REWARD>>": "reward_beat_tag",
                "<<FORWIN_IMMERSION>>": "immersion_anchor",
                "<<FORWIN_PROGRESS>>": "progress_marker",
                "<<FORWIN_MICRO_SUMMARY>>": "micro_summary",
                "<<FORWIN_CONTINUITY_ANCHOR>>": "continuity_anchor",
                "<<FORWIN_UNRESOLVED_HOOK>>": "unresolved_micro_hook",
                "<<FORWIN_NEXT_BRIDGE>>": "next_scene_bridge",
                "<<FORWIN_TIME_CONTINUITY>>": "time_continuity",
                "<<FORWIN_LOCATION_CONTINUITY>>": "location_continuity",
                "<<FORWIN_CHARACTER_FOCUS>>": "character_focus",
            },
        )
        involved_entities = self._parse_list_field(
            data.get("involved_entities", ""),
            default=scene_plan.involved_entities,
        )
        reward_tag = str(data.get("reward_beat_tag", "") or "").strip()
        if reward_tag not in _VALID_REWARD_TAGS:
            reward_tag = scene_plan.reward_beat_tag
        return SceneOutput(
            scene_no=scene_plan.scene_no,
            scene_objective=scene_plan.objective,
            scene_time_point=data.get("scene_time_point", scene_plan.time_hint),
            scene_location_id=data.get("scene_location_id", scene_plan.location_hint),
            involved_entities=involved_entities,
            text=str(data.get("body", data.get("text", "")) or ""),
            micro_summary=str(
                data.get("micro_summary", data.get("end_of_chapter_summary", "")) or ""
            ),
            reward_beat_tag=reward_tag,
            immersion_anchor=data.get("immersion_anchor", scene_plan.immersion_anchor),
            progress_marker=data.get("progress_marker", scene_plan.progress_marker),
            continuation=SceneContinuation(
                scene_no=scene_plan.scene_no,
                continuity_anchor=str(
                    data.get("continuity_anchor")
                    or scene_plan.micro_hook
                    or scene_plan.objective
                    or ""
                ).strip(),
                unresolved_micro_hook=str(
                    data.get("unresolved_micro_hook")
                    or scene_plan.micro_hook
                    or ""
                ).strip(),
                next_scene_bridge=str(data.get("next_scene_bridge") or "").strip(),
                time_continuity=str(
                    data.get("time_continuity")
                    or data.get("scene_time_point")
                    or scene_plan.time_hint
                    or ""
                ).strip(),
                location_continuity=str(
                    data.get("location_continuity")
                    or data.get("scene_location_id")
                    or scene_plan.location_hint
                    or ""
                ).strip(),
                character_focus=self._parse_list_field(
                    data.get("character_focus", ""),
                    default=involved_entities,
                ),
            ),
        )

    def _stitch_scenes(
        self,
        context: ChapterContextPack,
        scene_outputs: list[SceneOutput],
        *,
        skill_layers: list[object] | None = None,
    ) -> dict:
        raw_stitched = self._chat_preview_text(
            build_scene_stitch_prompt(context, scene_outputs, skill_layers=skill_layers),
            temperature=0.5,
            max_tokens=min(self.max_tokens, 2400),
            timeout_seconds=self.scene_call_timeout_seconds,
            max_attempts=2,
            retry_on_timeout=False,
        )
        return self._parse_preview_text(
            raw_stitched,
            fallback_title=context.chapter_plan_title or f"第{context.chapter_number}章",
        )

    def _extract_structured(
        self,
        context: ChapterContextPack,
        chapter_title: str,
        chapter_body: str,
    ) -> dict:
        metadata: dict[str, object] = {
            "new_events": [],
            "state_changes": [],
            "thread_beats": [],
            "time_advance": None,
            "lore_candidates": [],
            "timeline_hints": [],
            "writer_notes": [],
            "entity_mentions": [],
        }
        meta_notes: dict[str, object] = {
            "structured_extraction_calls": 3,
        }

        state_event = self._extract_structured_part(
            label="state_event_extraction",
            prompt_builder=build_state_event_extraction_prompt,
            context=context,
            chapter_title=chapter_title,
            chapter_body=chapter_body,
            primary_temperature=0.25,
            primary_max_tokens=min(self.max_tokens, 1000),
            retry_temperature=0.2,
            retry_max_tokens=min(self.max_tokens, 700),
        )
        thread_time = self._extract_structured_part(
            label="thread_time_extraction",
            prompt_builder=build_thread_time_extraction_prompt,
            context=context,
            chapter_title=chapter_title,
            chapter_body=chapter_body,
            primary_temperature=0.2,
            primary_max_tokens=min(self.max_tokens, 700),
            retry_temperature=0.15,
            retry_max_tokens=min(self.max_tokens, 520),
        )
        lore_timeline_notes = self._extract_structured_part(
            label="lore_timeline_notes_extraction",
            prompt_builder=build_lore_timeline_notes_extraction_prompt,
            context=context,
            chapter_title=chapter_title,
            chapter_body=chapter_body,
            primary_temperature=0.2,
            primary_max_tokens=min(self.max_tokens, 900),
            retry_temperature=0.15,
            retry_max_tokens=min(self.max_tokens, 560),
        )

        metadata.update(state_event)
        metadata.update(thread_time)
        metadata.update(lore_timeline_notes)
        for key, value in (
            state_event.get("_generation_meta") or {}
        ).items():
            meta_notes[key] = value
        for key, value in (
            thread_time.get("_generation_meta") or {}
        ).items():
            meta_notes[key] = value
        for key, value in (
            lore_timeline_notes.get("_generation_meta") or {}
        ).items():
            meta_notes[key] = value

        degraded_parts = [
            key
            for key in (
                "state_event_extraction",
                "thread_time_extraction",
                "lore_timeline_notes_extraction",
            )
            if meta_notes.get(key) == "degraded"
        ]
        if degraded_parts:
            meta_notes["structured_extraction"] = (
                "degraded" if len(degraded_parts) == 3 else "partial_degraded"
            )
            metadata["_generation_meta"] = meta_notes
        else:
            metadata["_generation_meta"] = meta_notes
        return metadata

    def _extract_structured_part(
        self,
        *,
        label: str,
        prompt_builder,
        context: ChapterContextPack,
        chapter_title: str,
        chapter_body: str,
        primary_temperature: float,
        primary_max_tokens: int,
        retry_temperature: float,
        retry_max_tokens: int,
    ) -> dict[str, object]:
        try:
            return self._chat_json(
                prompt_builder(context, chapter_title, chapter_body),
                temperature=primary_temperature,
                max_tokens=primary_max_tokens,
                timeout_seconds=self.scene_call_timeout_seconds,
                max_attempts=2,
                retry_on_timeout=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s primary pass failed, retrying with reduced body: %s", label, exc)
            shortened_body = chapter_body[: max(800, min(len(chapter_body), 1800))]
            try:
                return self._chat_json(
                    prompt_builder(context, chapter_title, shortened_body),
                    temperature=retry_temperature,
                    max_tokens=retry_max_tokens,
                    timeout_seconds=self.scene_call_timeout_seconds,
                    max_attempts=1,
                    retry_on_timeout=False,
                )
            except Exception as repair_exc:  # noqa: BLE001
                logger.warning("%s degraded to empty metadata after retry: %s", label, repair_exc)
                return {
                    "_generation_meta": {
                        label: "degraded",
                        f"{label}_error": str(repair_exc),
                    }
                }

    @staticmethod
    def _preview_summary(raw_summary: object, title: str, body: str) -> str:
        summary = str(raw_summary or "").strip()
        if summary:
            return summary
        cleaned = " ".join(
            line.strip()
            for line in body.replace("\r", "\n").split("\n")
            if line.strip()
        )
        if cleaned:
            return cleaned[:120]
        return title

    @staticmethod
    def _fallback_reward_tag(context: ChapterContextPack, index: int) -> str:
        plan = getattr(context, "chapter_experience_plan", None)
        tags = list(getattr(plan, "planned_reward_tags", []) or [])
        if tags:
            return str(tags[min(index, len(tags) - 1)])
        return "mystery"

    @staticmethod
    def _fallback_immersion_anchor(context: ChapterContextPack, index: int) -> str:
        plan = getattr(context, "chapter_experience_plan", None)
        anchors = list(getattr(plan, "immersion_anchors", []) or [])
        if anchors:
            return str(anchors[min(index, len(anchors) - 1)])
        timeline = getattr(getattr(context, "timeline", None), "current_time_label", "")
        return timeline or "沿用当前场景感官细节"

    @staticmethod
    def _fallback_progress_marker(context: ChapterContextPack, index: int) -> str:
        plan = getattr(context, "chapter_experience_plan", None)
        markers = list(getattr(plan, "progress_markers", []) or [])
        if markers:
            return str(markers[min(index, len(markers) - 1)])
        goals = context.chapter_goals or [context.chapter_plan_one_line or "推进主线"]
        return str(goals[min(index, len(goals) - 1)])

    @staticmethod
    def _parse_list_field(value: object, *, default: list[str]) -> list[str]:
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return items or list(default)
        text = str(value or "").strip()
        if not text:
            return list(default)
        parts = [
            item.strip(" -•\t")
            for item in re.split(r"[、,，/;\n]+", text.replace("\r", "\n"))
            if item.strip(" -•\t")
        ]
        return parts or list(default)

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
                raw = self._call_chat(
                    messages,
                    temperature=attempt["temperature"],
                    max_tokens=attempt["max_tokens"],
                    response_format={"type": "json_object"},
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

    def _chat_preview_text(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float | None = None,
        max_attempts: int = 2,
        retry_on_timeout: bool = True,
    ) -> str:
        attempts = [
            {"temperature": temperature, "max_tokens": max_tokens},
            {
                "temperature": max(0.2, temperature - 0.15),
                "max_tokens": max(600, min(max_tokens, 1400)),
            },
            {"temperature": 0.2, "max_tokens": max(520, min(max_tokens, 1100))},
        ][: max(1, int(max_attempts))]
        last_error: Exception | None = None
        for index, attempt in enumerate(attempts, start=1):
            try:
                raw = self._call_chat(
                    messages,
                    temperature=attempt["temperature"],
                    max_tokens=attempt["max_tokens"],
                    timeout_seconds=timeout_seconds,
                    retry_on_timeout=retry_on_timeout,
                )
                parsed = self._parse_preview_text(raw, fallback_title="")
                if str(parsed.get("body", "") or "").strip():
                    return raw
                raise ValueError("preview response body is empty")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "ChapterWriter preview call failed on attempt %d/%d: %s",
                    index,
                    len(attempts),
                    exc,
                )
        raise ValueError(f"ChapterWriter preview generation failed after retries: {last_error}")

    def _call_chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        response_format: dict | None = None,
        timeout_seconds: float | None = None,
        retry_on_timeout: bool = True,
    ) -> str:
        parameters = self._chat_signature.parameters
        kwargs: dict[str, object] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None and "response_format" in parameters:
            kwargs["response_format"] = response_format
        if timeout_seconds is not None and "timeout_seconds" in parameters:
            kwargs["timeout_seconds"] = timeout_seconds
        if "retry_on_timeout" in parameters:
            kwargs["retry_on_timeout"] = retry_on_timeout
        if "task_family" in parameters:
            kwargs["task_family"] = "writer"
        if "stage_key" in parameters:
            kwargs["stage_key"] = "chapter_draft"
        if "output_schema" in parameters and response_format is not None:
            kwargs["output_schema"] = {"type": "object"}
        return self.llm_client.chat(messages, **kwargs)

    def _attach_llm_fallback_events(self, output: WriterOutput) -> None:
        drain = getattr(self.llm_client, "drain_model_fallback_events", None)
        if not callable(drain):
            return
        events = drain()
        if events:
            output.generation_meta["model_fallbacks"] = events

    def _build_prompt_trace(
        self,
        *,
        base_messages: list[dict[str, str]],
        skill_layers: list[object] | None,
        template_id: str,
        stage_key: str,
        input_snapshot: dict[str, object],
        output_summary: dict[str, object],
    ) -> dict[str, object]:
        selected_skills = self._selected_skills_from_layers(skill_layers)
        prompt_layers = serialize_prompt_layers(base_messages, skill_layers or [])
        effective_system_prompt = "\n\n".join(
            str(item.get("content", "")).strip()
            for item in prompt_layers
            if str(item.get("role", "")).strip() == "system"
        )
        last_call_result = getattr(self.llm_client, "last_call_result", None)
        trace = getattr(last_call_result, "trace", {}) if last_call_result is not None else {}
        return {
            "trace_scope": "writer",
            "stage_key": stage_key,
            "backend": str(trace.get("backend", "") or getattr(last_call_result, "backend", "") or ""),
            "codex_job_id": str(trace.get("codex_job_id", "") or ""),
            "permission_profile": str(trace.get("permission_profile", "") or ""),
            "fallback_used": bool(getattr(last_call_result, "fallback_used", False)) if last_call_result is not None else False,
            "template_id": template_id,
            "template_version": "v1",
            "effective_system_prompt": effective_system_prompt,
            "prompt_layers": prompt_layers,
            "input_snapshot": {
                **input_snapshot,
                "stage_key": stage_key,
                "selected_skills": selected_skills,
            },
            "model_profile": {
                "profile_id": getattr(self.llm_client, "profile_id", ""),
                "profile_name": getattr(self.llm_client, "profile_name", ""),
                "model": getattr(self.llm_client, "model", ""),
                "base_url": getattr(self.llm_client, "base_url", ""),
            },
            "attempts": [],
            "output_summary": {
                **output_summary,
                "skill_summary": selected_skills,
            },
        }

    @staticmethod
    def _selected_skills_from_layers(skill_layers: list[object] | None) -> list[dict[str, str]]:
        payload: list[dict[str, str]] = []
        for item in skill_layers or []:
            payload.append(
                {
                    "id": str(getattr(item, "skill_id", getattr(item, "name", "")) or ""),
                    "version": str(getattr(item, "skill_version", getattr(item, "version", "")) or ""),
                    "hash": str(getattr(item, "skill_hash", "") or ""),
                    "path": str(getattr(item, "path", "") or ""),
                    "activation_reason": str(getattr(item, "activation_reason", "") or ""),
                    "mode": str(getattr(item, "mode", "") or ""),
                }
            )
        return [item for item in payload if item["id"]]

    @staticmethod
    def _parse_jsonish_text_payload(raw: str) -> dict[str, object]:
        cleaned = str(raw or "").strip()
        if not cleaned.startswith(("{", "[")):
            return {}
        try:
            data = parse_llm_json(cleaned, error_prefix="ChapterWriter tagged parser")
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(data, dict):
            return {}
        normalized: dict[str, object] = {}
        for key in (
            "title",
            "body",
            "text",
            "micro_summary",
            "end_of_chapter_summary",
            "scene_time_point",
            "scene_location_id",
            "reward_beat_tag",
            "immersion_anchor",
            "progress_marker",
            "continuity_anchor",
            "unresolved_micro_hook",
            "next_scene_bridge",
            "time_continuity",
            "location_continuity",
        ):
            if key in data and data.get(key) is not None:
                normalized[key] = str(data.get(key) or "").strip()
        if isinstance(data.get("involved_entities"), list):
            normalized["involved_entities"] = [
                str(item).strip() for item in data["involved_entities"] if str(item).strip()
            ]
        elif data.get("involved_entities") is not None:
            normalized["involved_entities"] = str(data.get("involved_entities") or "").strip()
        if isinstance(data.get("character_focus"), list):
            normalized["character_focus"] = [
                str(item).strip() for item in data["character_focus"] if str(item).strip()
            ]
        elif data.get("character_focus") is not None:
            normalized["character_focus"] = str(data.get("character_focus") or "").strip()
        if isinstance(data.get("continuation"), dict):
            continuation = data["continuation"]
            for source_key, target_key in (
                ("continuity_anchor", "continuity_anchor"),
                ("unresolved_micro_hook", "unresolved_micro_hook"),
                ("next_scene_bridge", "next_scene_bridge"),
                ("time_continuity", "time_continuity"),
                ("location_continuity", "location_continuity"),
            ):
                if continuation.get(source_key) is not None and target_key not in normalized:
                    normalized[target_key] = str(continuation.get(source_key) or "").strip()
            if isinstance(continuation.get("character_focus"), list) and "character_focus" not in normalized:
                normalized["character_focus"] = [
                    str(item).strip()
                    for item in continuation["character_focus"]
                    if str(item).strip()
                ]
        if not normalized.get("body") and normalized.get("text"):
            normalized["body"] = normalized["text"]
        if not normalized.get("end_of_chapter_summary") and normalized.get("micro_summary"):
            normalized["end_of_chapter_summary"] = normalized["micro_summary"]
        return normalized

    @classmethod
    def _parse_tagged_text(
        cls,
        raw: str,
        *,
        fallback_title: str,
        extra_markers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        cleaned = str(raw or "").strip()
        jsonish = cls._parse_jsonish_text_payload(cleaned)
        if jsonish.get("body"):
            if fallback_title and not jsonish.get("title"):
                jsonish["title"] = fallback_title
            return jsonish
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[^\n]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned).strip()

        marker_map = {
            "<<FORWIN_TITLE>>": "title",
            "<<FORWIN_BODY>>": "body",
            "<<FORWIN_SUMMARY>>": "end_of_chapter_summary",
            "【标题】": "title",
            "【正文】": "body",
            "【摘要】": "end_of_chapter_summary",
        }
        if extra_markers:
            marker_map.update(extra_markers)
        fields: dict[str, object] = {}
        pattern = "|".join(
            re.escape(marker)
            for marker in sorted(marker_map.keys(), key=len, reverse=True)
        )
        if pattern:
            matches = list(re.finditer(pattern, cleaned))
            for index, match in enumerate(matches):
                field_name = marker_map[match.group(0)]
                start = match.end()
                end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
                value = cleaned[start:end].strip()
                if value:
                    fields[field_name] = value

        if not fields.get("body"):
            title_match = re.search(r"标题[:：]\s*(.+)", cleaned)
            if title_match and not fields.get("title"):
                fields["title"] = title_match.group(1).strip()
            body_match = re.search(r"正文[:：]\s*(.+?)(?:摘要[:：]|$)", cleaned, re.S)
            if body_match:
                fields["body"] = body_match.group(1).strip()
            summary_match = re.search(r"摘要[:：]\s*(.+)$", cleaned, re.S)
            if summary_match and not fields.get("end_of_chapter_summary"):
                fields["end_of_chapter_summary"] = summary_match.group(1).strip()

        if not fields.get("body"):
            lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
            if lines:
                if not fields.get("title"):
                    fields["title"] = lines[0][:40]
                fields["body"] = "\n".join(lines[1:] if len(lines) > 1 else lines)

        body = str(fields.get("body", "") or "")
        if body and len(body.strip()) < 100 and len(cleaned) > 300:
            stripped_lines = []
            for line in cleaned.splitlines():
                candidate = line.strip()
                if not candidate:
                    continue
                if candidate.startswith(("<<FORWIN_", "【标题】", "【正文】", "【摘要】")):
                    continue
                if re.match(r"^[0-9]+\.\s", candidate):
                    continue
                if candidate in {"标题", "正文", "摘要"}:
                    continue
                stripped_lines.append(candidate)
            rebuilt = "\n".join(stripped_lines).strip()
            if len(rebuilt) > len(body):
                fields["body"] = rebuilt

        if fallback_title and not fields.get("title"):
            fields["title"] = fallback_title
        return fields

    @classmethod
    def _parse_preview_text(cls, raw: str, *, fallback_title: str) -> dict[str, str]:
        fields = cls._parse_tagged_text(raw, fallback_title=fallback_title)
        return {
            "title": str(fields.get("title", fallback_title) or fallback_title),
            "body": str(fields.get("body", "") or "").strip(),
            "end_of_chapter_summary": str(
                fields.get(
                    "end_of_chapter_summary",
                    fields.get("micro_summary", ""),
                )
                or ""
            ).strip(),
        }
