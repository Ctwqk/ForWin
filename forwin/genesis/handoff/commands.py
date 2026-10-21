from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class HandoffStageError(ValueError):
    """A typed failure of one start-writing handoff stage.

    The handoff rolls back its writes and records the stage's failure event; the
    caller commits that event and refuses the request.
    """


@dataclass(slots=True)
class StartWritingCommand:
    project_id: str
    actor_type: str = "manual_ui"
    runtime_config: Any | None = None


@dataclass(slots=True)
class StartWritingHandoffResult:
    project_id: str
    active_arc_id: str
    active_arc_number: int
    created_arc_count: int
    created_chapter_plan_count: int
    active_chapter_plan_count: int
    map_bootstrap_summary: dict[str, Any] = field(default_factory=dict)
    world_seed_summary: dict[str, Any] = field(default_factory=dict)
    project_status: str = "writing"

