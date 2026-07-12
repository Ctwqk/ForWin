from __future__ import annotations

from dataclasses import dataclass

from forwin.config import InfrastructureConfig
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.services import RuntimeServices


@dataclass(slots=True)
class WorkerRuntime:
    services: RuntimeServices

    def close(self) -> None:
        self.services.llm_client.close()
        self.services.engine.dispose()


def build_generation_worker_runtime(
    config: InfrastructureConfig,
) -> WorkerRuntime:
    container = RuntimeContainer.for_generation_worker(
        config,
        policy=RuntimePolicy.for_profile("standard"),
    )
    return WorkerRuntime(container.services())


def build_publisher_worker_runtime(
    config: InfrastructureConfig,
) -> WorkerRuntime:
    container = RuntimeContainer.for_publisher_worker(
        config,
        policy=RuntimePolicy.for_profile("standard"),
    )
    return WorkerRuntime(container.services())


__all__ = [
    "WorkerRuntime",
    "build_generation_worker_runtime",
    "build_publisher_worker_runtime",
]
