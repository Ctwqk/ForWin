from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from forwin.config import InfrastructureConfig
from forwin.generation.pipeline import ChapterPipeline
from forwin.models.project import Project
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy_store import ProjectPolicyStore


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProjectPipelineCache:
    """API-role pipelines for projects whose RuntimePolicy differs from the default.

    The HTTP runtime's shared pipeline is built with the API container's default
    policy. Canon admission must instead honor each project's versioned policy,
    as the generation worker does with each task's policy snapshot.
    """

    _pipelines: dict[str, ChapterPipeline] = field(default_factory=dict)
    _containers: list[RuntimeContainer] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def resolve(
        self,
        project_id: str,
        shared: ChapterPipeline,
        get_session: Callable[[], Session],
        config: InfrastructureConfig | None,
    ) -> ChapterPipeline:
        with get_session() as session:
            project = session.get(Project, project_id)
            if project is None:
                return shared
            policy = ProjectPolicyStore(session).load(project).policy
        if policy == shared.policy:
            return shared
        key = policy.model_dump_json()
        with self._lock:
            pipeline = self._pipelines.get(key)
            if pipeline is None:
                container = RuntimeContainer.for_api(config, policy=policy)
                try:
                    pipeline = container.build_chapter_pipeline()
                except Exception:
                    container.close()
                    raise
                self._containers.append(container)
                self._pipelines[key] = pipeline
            return pipeline

    def close(self) -> None:
        for container in self._containers:
            try:
                container.close()
            except Exception:  # noqa: BLE001
                logger.debug("Ignoring project pipeline shutdown error.", exc_info=True)
        self._containers.clear()
        self._pipelines.clear()


__all__ = ["ProjectPipelineCache"]
