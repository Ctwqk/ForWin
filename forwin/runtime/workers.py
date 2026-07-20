from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from forwin.application.generation import GenerationApplicationService
from forwin.config import InfrastructureConfig
from forwin.outbox.worker import OutboxClaim
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy


@dataclass(slots=True)
class GenerationWorkerRuntime:
    container: RuntimeContainer
    application_service: GenerationApplicationService

    def close(self) -> None:
        self.container.close()


@dataclass(slots=True)
class PublisherWorkerRuntime:
    container: RuntimeContainer
    publisher_runtime: Any

    def close(self) -> None:
        self.container.close()


@dataclass(slots=True)
class OutboxWorkerRuntime:
    container: RuntimeContainer
    session_factory: Callable[[], Any]
    handlers: dict[str, Callable[[OutboxClaim], None]]

    def close(self) -> None:
        self.container.close()


def build_generation_worker_runtime(
    config: InfrastructureConfig,
) -> GenerationWorkerRuntime:
    container = RuntimeContainer.for_generation_worker(
        config,
        policy=RuntimePolicy.for_profile("standard"),
    )
    try:
        core = container.core_services()
        return GenerationWorkerRuntime(
            container=container,
            application_service=core.generation_application,
        )
    except Exception:
        container.close()
        raise


def build_publisher_worker_runtime(
    config: InfrastructureConfig,
) -> PublisherWorkerRuntime:
    container = RuntimeContainer.for_publisher_worker(
        config,
        policy=RuntimePolicy.for_profile("standard"),
    )
    try:
        publisher = container.publisher_services()
        return PublisherWorkerRuntime(
            container=container,
            publisher_runtime=publisher.publisher_runtime,
        )
    except Exception:
        container.close()
        raise


def build_outbox_worker_runtime(
    config: InfrastructureConfig,
) -> OutboxWorkerRuntime:
    container = RuntimeContainer.for_outbox_worker(
        config,
        policy=RuntimePolicy.for_profile("standard"),
    )
    try:
        core = container.core_services()
        return OutboxWorkerRuntime(
            container=container,
            session_factory=core.session_factory,
            handlers=container.build_outbox_handlers(),
        )
    except Exception:
        container.close()
        raise


__all__ = [
    "GenerationWorkerRuntime",
    "OutboxWorkerRuntime",
    "PublisherWorkerRuntime",
    "build_generation_worker_runtime",
    "build_outbox_worker_runtime",
    "build_publisher_worker_runtime",
]
