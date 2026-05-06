from __future__ import annotations

from typing import Protocol

from forwin.config import Config


class RuntimeContainerPort(Protocol):
    @classmethod
    def from_config(cls, config: Config) -> "RuntimeContainerPort":
        ...

    def services(self):
        ...

    def build_writing_orchestrator(
        self,
        *,
        progress_callback=None,
        should_abort=None,
        should_pause=None,
    ):
        ...
