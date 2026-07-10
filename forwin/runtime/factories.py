from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.config import InfrastructureConfig
from forwin.production.scheduler import ProductionScheduler
from forwin.runtime.policy import RuntimePolicy
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.profile import WriterProfile


@dataclass(slots=True)
class ProductionSchedulerFactory:
    session_factory: Any
    infrastructure: InfrastructureConfig
    observability: Any = None

    def build(self, **callbacks) -> ProductionScheduler:
        return ProductionScheduler(
            session_factory=self.session_factory,
            config=self.infrastructure,
            observability=self.observability,
            **callbacks,
        )


def build_writer(
    infrastructure: InfrastructureConfig,
    policy: RuntimePolicy,
    llm_client,
    observability=None,
) -> ChapterWriter:
    return ChapterWriter(
        llm_client=llm_client,
        writer_mode="scene",
        single_call_timeout_seconds=infrastructure.llm_timeout_seconds,
        scene_call_timeout_seconds=infrastructure.scene_call_timeout_seconds,
        observability=observability,
        profile=infrastructure.writer_profile(policy),
    )


def build_provisional_writer(
    infrastructure: InfrastructureConfig,
    policy: RuntimePolicy,
    llm_client,
    observability=None,
) -> ChapterWriter:
    lengths = policy.chapter_length
    provisional_target_chars = max(700, min(lengths.target_chars, 900))
    provisional_min_chars = max(500, min(lengths.min_chars, provisional_target_chars))
    provisional_max_chars = max(
        provisional_target_chars,
        min(lengths.max_chars, 1000),
    )
    provisional_timeout_seconds = min(
        max(
            infrastructure.llm_timeout_seconds,
            infrastructure.scene_call_timeout_seconds,
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
            temperature=min(infrastructure.temperature, 0.7),
            max_tokens=min(infrastructure.max_tokens, 2400),
            default_scene_count=1,
            max_scene_count=1,
            min_chapter_chars=provisional_min_chars,
            max_chapter_chars=provisional_max_chars,
            target_chapter_chars=provisional_target_chars,
            prompt_budget_chars=infrastructure.prompt_budget_chars,
        ),
    )
