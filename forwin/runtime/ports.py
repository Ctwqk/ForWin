from __future__ import annotations

from typing import Protocol

from forwin.config import InfrastructureConfig
from forwin.runtime.policy import RuntimePolicy


class RuntimeContainerPort(Protocol):
    @classmethod
    def from_config(
        cls,
        infrastructure: InfrastructureConfig,
        *,
        policy: RuntimePolicy,
        role: str = "full",
    ) -> "RuntimeContainerPort":
        ...

    def services(self):
        ...

    def build_chapter_pipeline(
        self,
        *,
        progress_callback=None,
        should_abort=None,
        should_pause=None,
        task_id: str = "",
        root_event_id: str = "",
    ):
        ...
