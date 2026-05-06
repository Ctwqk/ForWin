from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.config import Config
from forwin.production.scheduler import ProductionScheduler
from forwin.writer.chapter_writer import ChapterWriter


@dataclass(slots=True)
class ProductionSchedulerFactory:
    session_factory: Any
    config: Config

    def build(self, **callbacks) -> ProductionScheduler:
        return ProductionScheduler(
            session_factory=self.session_factory,
            config=self.config,
            **callbacks,
        )


def build_writer(config: Config, llm_client) -> ChapterWriter:
    return ChapterWriter(
        llm_client=llm_client,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        writer_mode=config.writer_mode,
        default_scene_count=config.default_scene_count,
        max_scene_count=config.max_scene_count,
        min_chapter_chars=config.min_chapter_chars,
        max_chapter_chars=config.max_chapter_chars,
        target_chapter_chars=config.target_chapter_chars,
        single_call_timeout_seconds=config.llm_timeout_seconds,
        scene_call_timeout_seconds=config.scene_call_timeout_seconds,
    )


def build_provisional_writer(config: Config, llm_client) -> ChapterWriter:
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
        temperature=min(config.temperature, 0.7),
        max_tokens=min(config.max_tokens, 2400),
        writer_mode="single",
        default_scene_count=1,
        max_scene_count=1,
        min_chapter_chars=provisional_min_chars,
        max_chapter_chars=provisional_max_chars,
        target_chapter_chars=provisional_target_chars,
        single_call_timeout_seconds=provisional_timeout_seconds,
        scene_call_timeout_seconds=provisional_timeout_seconds,
    )
