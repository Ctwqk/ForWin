from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.config import InfrastructureConfig
from forwin.production.scheduler import ProductionScheduler
from forwin.runtime.policy import RuntimePolicy
from forwin.writer.chapter_writer import ChapterWriter


@dataclass(slots=True)
class ProductionSchedulerFactory:
    session_factory: Any
    infrastructure: InfrastructureConfig
    generation_application: Any
    observability: Any = None

    def build(self, **callbacks) -> ProductionScheduler:
        return ProductionScheduler(
            session_factory=self.session_factory,
            config=self.infrastructure,
            generation_application=self.generation_application,
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
