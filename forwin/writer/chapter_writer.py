from __future__ import annotations

import inspect
import logging
import re
from contextlib import contextmanager

from forwin.chapter_titles import rebase_generic_numeric_chapter_title
from forwin.model_adapter import ModelAdapter
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneContinuation, SceneOutput, ScenePlan, is_scene_location_ref
from forwin.protocol.writer import WriterOutput
from forwin.skills import serialize_prompt_layers, summarize_skill_layers
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure
from forwin.observability.context import OperationContext
from forwin.observability.ports import NullObservability
from forwin.writer.prompt_budget import prompt_revision_hash
from forwin.writer.profile import WriterProfile
from .prompt_core import (
    build_preview_chapter_prompt,
    build_single_chapter_draft_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
)
from .prompt_core.world_view import view_location_rows
from forwin.utils import parse_llm_json

logger = logging.getLogger(__name__)
_VALID_REWARD_TAGS = {"power", "social", "justice", "mystery", "emotion"}
_BODY_TERMINAL_PUNCTUATION = set("。！？!?…")
_BODY_TRAILING_CLOSERS = set("”’」』）)]》】")


class ChapterWriter:
    """Generates chapters using LLM.

    Usage::

        client = LLMClient(api_key="...")
        writer = ChapterWriter(client)
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
        scene_call_timeout_seconds: float = 90.0,
        observability=None,
        profile: WriterProfile | None = None,
    ) -> None:
        self.profile = profile or WriterProfile.from_values(
            temperature=temperature,
            max_tokens=max_tokens,
            default_scene_count=default_scene_count,
            max_scene_count=max_scene_count,
            min_chapter_chars=min_chapter_chars,
            max_chapter_chars=max_chapter_chars,
            target_chapter_chars=target_chapter_chars,
        )
        self.llm_client = llm_client
        self.temperature = self.profile.temperature
        self.max_tokens = self.profile.max_tokens
        self.writer_mode = writer_mode
        self.default_scene_count = self.profile.default_scene_count
        self.max_scene_count = self.profile.max_scene_count
        self.min_chapter_chars = self.profile.min_chapter_chars
        self.max_chapter_chars = self.profile.max_chapter_chars
        self.target_chapter_chars = self.profile.target_chapter_chars
        self.single_call_timeout_seconds = max(10.0, float(single_call_timeout_seconds))
        self.scene_call_timeout_seconds = max(10.0, float(scene_call_timeout_seconds))
        self._chat_signature = inspect.signature(self.llm_client.chat)
        self._business_retry_events: list[dict[str, object]] = []
        self._llm_route_overrides: list[dict[str, str]] = []
        self.observability = observability or NullObservability()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_chapter(
        self,
        context: ChapterContextPack,
        *,
        skill_layers: list[object] | None = None,
        trace_stage_key: str = "writer_preview",
        llm_preferred_provider_kind: str = "",
        llm_preferred_model: str = "",
    ) -> WriterOutput:
        """Write a single chapter.

        Returns a fully-populated WriterOutput.  The char_count field is
        computed from the body after parsing.
        """
        obs_context = OperationContext(
            project_id=getattr(context, "project_id", ""),
            chapter_number=int(getattr(context, "chapter_number", 0) or 0),
            stage=trace_stage_key,
        )
        with self.observability.span(
            obs_context,
            "writer.write_chapter",
            span_kind="writer",
            component="writer",
            tags={"writer_mode": self.writer_mode},
        ) as span:
            with self._llm_route_override(
                preferred_provider_kind=llm_preferred_provider_kind,
                preferred_model=llm_preferred_model,
            ):
                if self.writer_mode == "single":
                    output = self._write_single_chapter(
                        context,
                        skill_layers=skill_layers,
                        trace_stage_key=trace_stage_key,
                    )
                else:
                    output = self._write_scene_chapter(
                        context,
                        skill_layers=skill_layers,
                        trace_stage_key=trace_stage_key,
                    )
            span.metric("char_count", int(getattr(output, "char_count", 0) or 0))
            span.metric("scene_count", len(getattr(output, "scene_outputs", []) or []))
            return output

    @contextmanager
    def _llm_route_override(
        self,
        *,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ):
        override = {
            "preferred_provider_kind": str(preferred_provider_kind or "").strip(),
            "preferred_model": str(preferred_model or "").strip(),
        }
        self._llm_route_overrides.append(override)
        try:
            yield
        finally:
            self._llm_route_overrides.pop()

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
        """Write a lightweight fallback preview chapter.

        Preview generation prioritizes producing a readable draft with a
        single LLM call, so writer fallback can retain real preview text.
        """
        logger.info(
            "write_chapter(preview): chapter=%d title_plan=%r",
            context.chapter_number,
            context.chapter_plan_title,
        )
        obs_context = OperationContext(
            project_id=getattr(context, "project_id", ""),
            chapter_number=int(getattr(context, "chapter_number", 0) or 0),
            stage=trace_stage_key,
        )
        with self.observability.span(
            obs_context,
            "writer.preview_chapter",
            span_kind="writer",
            component="writer",
            tags={"writer_mode": "preview"},
        ) as span:
            output = self._write_preview_chapter_inner(
                context,
                skill_layers=skill_layers,
                trace_stage_key=trace_stage_key,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                retry_on_timeout=retry_on_timeout,
            )
            span.metric("char_count", int(getattr(output, "char_count", 0) or 0))
            return output

    def _write_preview_chapter_inner(
        self,
        context: ChapterContextPack,
        *,
        skill_layers: list[object] | None,
        trace_stage_key: str,
        timeout_seconds: float | None,
        max_attempts: int,
        retry_on_timeout: bool,
    ) -> WriterOutput:
        """Implementation body for preview writing so the public method can wrap a span."""
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
        actual_messages = build_preview_chapter_prompt(
            context,
            target_chars=target_chars,
            min_chars=self.min_chapter_chars,
            max_chars=self.max_chapter_chars,
            skill_layers=skill_layers,
        )
        preview_text = self._chat_preview_text(
            actual_messages,
            temperature=min(self.temperature, 0.7),
            max_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds or self.single_call_timeout_seconds,
            max_attempts=max_attempts,
            retry_on_timeout=retry_on_timeout,
            stage_key=trace_stage_key,
            min_body_chars=self.min_chapter_chars,
        )
        draft_data = self._parse_preview_text(
            preview_text,
            fallback_title=context.chapter_plan_title
            or f"第{context.chapter_number}章",
        )
        title = draft_data.get(
            "title",
            context.chapter_plan_title or f"第{context.chapter_number}章",
        )
        title = rebase_generic_numeric_chapter_title(title, context.chapter_number)
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
                "mode": "writer_preview",
                "call_count": 1,
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
                        "mode": "writer_preview",
                        "title": title,
                        "char_count": len(body),
                    },
                ),
            },
        )
        self._attach_prompt_revision(output, actual_messages)
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
        actual_messages = build_single_chapter_draft_prompt(
            context,
            target_chars=target_chars,
            min_chars=self.min_chapter_chars,
            max_chars=self.max_chapter_chars,
            skill_layers=skill_layers,
        )
        raw_draft = self._chat_preview_text(
            actual_messages,
            temperature=self.temperature,
            max_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds or self.single_call_timeout_seconds,
            max_attempts=max_attempts,
            retry_on_timeout=retry_on_timeout,
            stage_key=trace_stage_key,
            min_body_chars=self.min_chapter_chars,
        )
        draft_data = self._parse_preview_text(
            raw_draft,
            fallback_title=context.chapter_plan_title
            or f"第{context.chapter_number}章",
        )
        output = self._writer_output_from_dict(context, draft_data)
        output.generation_meta.update(
            {
                "mode": "single",
                "call_count": 1,
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
        self._attach_prompt_revision(output, actual_messages)
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
            stitched = self._stitch_scenes(
                context, scene_outputs, skill_layers=skill_layers
            )
            output = self._writer_output_from_dict(
                context, stitched, scene_outputs=scene_outputs
            )
            self._collect_scene_locations(context, output)
            output.generation_meta.update(
                {
                    "mode": "scene",
                    "scene_count": len(scene_outputs),
                    # Scene breakdown, one call per scene and the stitch.
                    "call_count": len(scene_outputs) + 2,
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
            self._attach_prompt_revision(output, base_messages)
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
                max_attempts=2,
                retry_on_timeout=True,
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
        body = data.get("body", "")
        output = WriterOutput(
            project_id=getattr(context, "project_id", ""),
            chapter_number=context.chapter_number,
            title=rebase_generic_numeric_chapter_title(
                data.get("title", f"第{context.chapter_number}章"),
                context.chapter_number,
            ),
            body=body,
            char_count=len(body) if isinstance(body, str) else 0,
            end_of_chapter_summary=data.get("end_of_chapter_summary", ""),
            scene_outputs=scene_outputs or [],
        )
        self._attach_llm_fallback_events(output)
        logger.info(
            "write_chapter: done – chapter=%d char_count=%d",
            output.chapter_number,
            output.char_count,
        )
        return output

    @staticmethod
    def _collect_scene_locations(context: ChapterContextPack, output: WriterOutput) -> None:
        """Gathers the scenes' declared locations as hints for extraction.

        A location that is neither an id from the world view's location table nor ``new:<name>``
        stays as written, with a writer warning.
        """
        view = getattr(context, "world_view", None)
        location_ids = {row.id for row in view_location_rows(view)} if view is not None else set()
        warnings: list[dict[str, object]] = []
        for scene in output.scene_outputs:
            value = scene.scene_location_id.strip()
            if value and not is_scene_location_ref(value, location_ids):
                logger.warning(
                    "chapter %d scene %d location %r is neither a view location id nor new:<name>",
                    context.chapter_number,
                    scene.scene_no,
                    value,
                )
                warnings.append(
                    {"code": "scene_location_not_in_view", "scene_no": scene.scene_no, "value": value}
                )
        output.scene_location_hints = list(
            dict.fromkeys(
                scene.scene_location_id.strip()
                for scene in output.scene_outputs
                if scene.scene_location_id.strip()
            )
        )
        if warnings:
            output.generation_meta["writer_warnings"] = warnings

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
                stage_key="scene_breakdown",
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
        goals = context.chapter_goals or [
            context.chapter_plan_one_line or "推进本章主线"
        ]
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
            build_scene_generation_prompt(
                context, scene_plan, skill_layers=skill_layers
            ),
            temperature=self.temperature,
            max_tokens=max_output_tokens,
            timeout_seconds=self.scene_call_timeout_seconds,
            max_attempts=1,
            retry_on_timeout=False,
            stage_key="scene_generation",
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
                    data.get("unresolved_micro_hook") or scene_plan.micro_hook or ""
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
            build_scene_stitch_prompt(
                context,
                scene_outputs,
                target_chars=self.target_chapter_chars,
                min_chars=self.min_chapter_chars,
                max_chars=self.max_chapter_chars,
                skill_layers=skill_layers,
            ),
            temperature=0.5,
            max_tokens=min(
                self.max_tokens,
                max(5200, int(self.target_chapter_chars * 2.0)),
            ),
            timeout_seconds=self.scene_call_timeout_seconds,
            max_attempts=1,
            retry_on_timeout=False,
            stage_key="scene_stitch",
            min_body_chars=self.min_chapter_chars,
        )
        return self._parse_preview_text(
            raw_stitched,
            fallback_title=context.chapter_plan_title
            or f"第{context.chapter_number}章",
        )

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
        stage_key: str = "writer_json",
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
            raw = ""
            try:
                raw = self._call_chat(
                    messages,
                    temperature=attempt["temperature"],
                    max_tokens=attempt["max_tokens"],
                    response_format={"type": "json_object"},
                    timeout_seconds=timeout_seconds,
                    retry_on_timeout=retry_on_timeout,
                    stage_key=stage_key,
                )
                try:
                    return parse_llm_json(
                        raw,
                        error_prefix="ChapterWriter JSON parser",
                    )
                except Exception as exc:  # noqa: BLE001
                    mark_latest_attempt_parse_failure(
                        self.llm_client,
                        parser_name="ChapterWriter JSON parser",
                        stage_key=stage_key,
                        schema_name="writer_json",
                        raw_output=raw,
                        error=exc,
                    )
                    raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._record_business_retry_event(
                    stage="json_generation",
                    attempt_no=index,
                    max_attempts=len(attempts),
                    reason=exc,
                )
                logger.warning(
                    "ChapterWriter JSON call failed on attempt %d/%d: %s",
                    index,
                    len(attempts),
                    exc,
                )
        raise ValueError(
            f"ChapterWriter JSON generation failed after retries: {last_error}"
        )

    def _chat_preview_text(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float | None = None,
        max_attempts: int = 2,
        retry_on_timeout: bool = True,
        stage_key: str = "chapter_draft",
        min_body_chars: int = 0,
    ) -> str:
        attempts = [
            {"temperature": temperature, "max_tokens": max_tokens},
            {
                "temperature": max(0.2, temperature - 0.15),
                "max_tokens": max_tokens,
            },
            {"temperature": 0.2, "max_tokens": max_tokens},
        ][: max(1, int(max_attempts))]
        last_error: Exception | None = None
        for index, attempt in enumerate(attempts, start=1):
            raw = ""
            try:
                raw = self._call_chat(
                    messages,
                    temperature=attempt["temperature"],
                    max_tokens=attempt["max_tokens"],
                    timeout_seconds=timeout_seconds,
                    retry_on_timeout=retry_on_timeout,
                    stage_key=stage_key,
                )
                parsed = self._parse_preview_text(raw, fallback_title="")
                body = str(parsed.get("body", "") or "").strip()
                if body:
                    if not self._body_looks_complete(body):
                        raise ValueError("preview response body appears incomplete")
                    required_chars = max(0, int(min_body_chars or 0))
                    if len(body) < required_chars:
                        raise ValueError(
                            f"preview response body has {len(body)} chars, "
                            f"below minimum {required_chars}"
                        )
                    return raw
                raise ValueError("preview response body is empty")
            except Exception as exc:  # noqa: BLE001
                if raw:
                    mark_latest_attempt_parse_failure(
                        self.llm_client,
                        parser_name="ChapterWriter preview parser",
                        stage_key=stage_key,
                        schema_name="writer_preview",
                        raw_output=raw,
                        error=exc,
                    )
                last_error = exc
                self._record_business_retry_event(
                    stage="preview_generation",
                    attempt_no=index,
                    max_attempts=len(attempts),
                    reason=exc,
                )
                logger.warning(
                    "ChapterWriter preview call failed on attempt %d/%d: %s",
                    index,
                    len(attempts),
                    exc,
                )
        raise ValueError(
            f"ChapterWriter preview generation failed after retries: {last_error}"
        )

    @staticmethod
    def _body_looks_complete(body: str) -> bool:
        text = str(body or "").strip()
        if not text:
            return False
        while text and text[-1] in _BODY_TRAILING_CLOSERS:
            text = text[:-1].rstrip()
        return bool(text and text[-1] in _BODY_TERMINAL_PUNCTUATION)

    def _call_chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        response_format: dict | None = None,
        timeout_seconds: float | None = None,
        retry_on_timeout: bool = True,
        task_family: str = "writer",
        stage_key: str = "chapter_draft",
        output_schema: dict | None = None,
    ) -> str:
        parameters = self._chat_signature.parameters
        kwargs: dict[str, object] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        route_override = (
            self._llm_route_overrides[-1] if self._llm_route_overrides else {}
        )
        if response_format is not None and "response_format" in parameters:
            kwargs["response_format"] = response_format
        if timeout_seconds is not None and "timeout_seconds" in parameters:
            kwargs["timeout_seconds"] = timeout_seconds
        if "retry_on_timeout" in parameters:
            kwargs["retry_on_timeout"] = retry_on_timeout
        if "task_family" in parameters:
            kwargs["task_family"] = task_family
        if "stage_key" in parameters:
            kwargs["stage_key"] = stage_key
        if "output_schema" in parameters and output_schema is not None:
            kwargs["output_schema"] = output_schema
        if route_override:
            preferred_provider_kind = route_override.get("preferred_provider_kind", "")
            preferred_model = route_override.get("preferred_model", "")
            if preferred_provider_kind and "preferred_provider_kind" in parameters:
                kwargs["preferred_provider_kind"] = preferred_provider_kind
            if preferred_model and "preferred_model" in parameters:
                kwargs["preferred_model"] = preferred_model
        return self.llm_client.chat(messages, **kwargs)

    def _record_business_retry_event(
        self,
        *,
        stage: str,
        attempt_no: int,
        max_attempts: int,
        reason: BaseException,
    ) -> None:
        self._business_retry_events.append(
            {
                "stage": stage,
                "attempt_no": int(attempt_no),
                "max_attempts": int(max_attempts),
                "reason_class": reason.__class__.__name__,
                "reason": str(reason)[:500],
                "retry_kind": "business_validation",
            }
        )

    def _attach_llm_fallback_events(self, output: WriterOutput) -> None:
        drain = getattr(self.llm_client, "drain_model_fallback_events", None)
        if not callable(drain):
            return
        events = drain()
        if events:
            output.generation_meta["model_fallbacks"] = events

    @staticmethod
    def _attach_prompt_revision(
        output: WriterOutput, messages: list[dict[str, object]]
    ) -> None:
        revision_hash = prompt_revision_hash(messages)
        output.prompt_revision_hash = revision_hash
        output.generation_meta["prompt_revision_hash"] = revision_hash

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
        selected_skills = summarize_skill_layers(skill_layers)
        drain_attempts = getattr(self.llm_client, "drain_llm_attempt_events", None)
        attempts = drain_attempts() if callable(drain_attempts) else []
        business_retry_events = list(self._business_retry_events)
        self._business_retry_events.clear()
        prompt_layers = serialize_prompt_layers(base_messages, skill_layers or [])
        effective_system_prompt = "\n\n".join(
            str(item.get("content", "")).strip()
            for item in prompt_layers
            if str(item.get("role", "")).strip() == "system"
        )
        last_call_result = getattr(self.llm_client, "last_call_result", None)
        trace = (
            getattr(last_call_result, "trace", {})
            if last_call_result is not None
            else {}
        )
        return {
            "trace_scope": "writer",
            "stage_key": stage_key,
            "backend": str(
                trace.get("backend", "")
                or getattr(last_call_result, "backend", "")
                or ""
            ),
            "codex_job_id": str(trace.get("codex_job_id", "") or ""),
            "permission_profile": str(trace.get("permission_profile", "") or ""),
            "fallback_used": bool(getattr(last_call_result, "fallback_used", False))
            if last_call_result is not None
            else False,
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
            "attempts": attempts if isinstance(attempts, list) else [],
            "output_summary": {
                **output_summary,
                "skill_summary": selected_skills,
                "business_retry_events": business_retry_events,
            },
        }

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
                str(item).strip()
                for item in data["involved_entities"]
                if str(item).strip()
            ]
        elif data.get("involved_entities") is not None:
            normalized["involved_entities"] = str(
                data.get("involved_entities") or ""
            ).strip()
        if isinstance(data.get("character_focus"), list):
            normalized["character_focus"] = [
                str(item).strip()
                for item in data["character_focus"]
                if str(item).strip()
            ]
        elif data.get("character_focus") is not None:
            normalized["character_focus"] = str(
                data.get("character_focus") or ""
            ).strip()
        if isinstance(data.get("continuation"), dict):
            continuation = data["continuation"]
            for source_key, target_key in (
                ("continuity_anchor", "continuity_anchor"),
                ("unresolved_micro_hook", "unresolved_micro_hook"),
                ("next_scene_bridge", "next_scene_bridge"),
                ("time_continuity", "time_continuity"),
                ("location_continuity", "location_continuity"),
            ):
                if (
                    continuation.get(source_key) is not None
                    and target_key not in normalized
                ):
                    normalized[target_key] = str(
                        continuation.get(source_key) or ""
                    ).strip()
            if (
                isinstance(continuation.get("character_focus"), list)
                and "character_focus" not in normalized
            ):
                normalized["character_focus"] = [
                    str(item).strip()
                    for item in continuation["character_focus"]
                    if str(item).strip()
                ]
        if not normalized.get("body") and normalized.get("text"):
            normalized["body"] = normalized["text"]
        if not normalized.get("end_of_chapter_summary") and normalized.get(
            "micro_summary"
        ):
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
                end = (
                    matches[index + 1].start()
                    if index + 1 < len(matches)
                    else len(cleaned)
                )
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
                if candidate.startswith(
                    ("<<FORWIN_", "【标题】", "【正文】", "【摘要】")
                ):
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
