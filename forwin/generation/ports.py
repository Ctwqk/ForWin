from __future__ import annotations

from collections.abc import Callable
from typing import Any


CreateContinueGenerationTask = Callable[..., str]
GenerationTaskRunner = Callable[..., Any]
