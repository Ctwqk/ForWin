from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.config import Config
from forwin.production.scheduler import ProductionScheduler
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.profile import WriterProfile


@dataclass(slots=True)
class ProductionSchedulerFactory:
    session_factory: Any
    config: Config
    observability: Any = None

    def build(self, **callbacks) -> ProductionScheduler:
        return ProductionScheduler(
            session_factory=self.session_factory,
            config=self.config,
            observability=self.observability,
            **callbacks,
        )


def build_writer(config: Config, llm_client, observability=None) -> ChapterWriter:
    return ChapterWriter(
        llm_client=llm_client,
        writer_mode=config.writer_mode,
        single_call_timeout_seconds=config.llm_timeout_seconds,
        scene_call_timeout_seconds=config.scene_call_timeout_seconds,
        observability=observability,
        profile=config.writer,
    )


def build_provisional_writer(config: Config, llm_client, observability=None) -> ChapterWriter:
    provisional_target_chars = max(700, min(config.target_chapter_chars, 900))
    provisional_min_chars = max(500, min(config.min_chapter_chars, provisional_target_chars))
    provisional_max_chars = max(
        provisional_target_chars,
        min(config.max_chapter_chars, 1000),
    )
    provisional_timeout_seconds = min(
        max(
            config.llm_timeout_seconds,
            config.scene_call_timeout_seconds,
            90.0,
        ),
        180.0,
    )
    return ChapterWriter(
        llm_client=llm_client,
        writer_mode="single",
        single_call_timeout_seconds=provisional_timeout_seconds,
        scene_call_timeout_seconds=provisional_timeout_seconds,
        observability=observability,
        profile=WriterProfile.from_values(
            temperature=min(config.temperature, 0.7),
            max_tokens=min(config.max_tokens, 2400),
            default_scene_count=1,
            max_scene_count=1,
            min_chapter_chars=provisional_min_chars,
            max_chapter_chars=provisional_max_chars,
            target_chapter_chars=provisional_target_chars,
            prompt_budget_chars=getattr(config, "prompt_budget_chars", 12000),
        ),
    )
