from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ChapterPipelinePorts:
    context_assembler: Any
    writer: Any
    review_hub: Any
    repair_policy: Any
    repair_verifier: Any
    final_acceptance_gate: Any
