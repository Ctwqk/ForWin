from __future__ import annotations

from typing import Any

from .base import PromptJsonAnalyzer
from .schemas import common_output_schema

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Analyze whether the writer output changes or contradicts known character states.

Character state includes:
- location
- physical health
- mental state
- knowledge
- allegiance
- relationship
- disguise or identity presentation
- capability
- inventory
- life/death/status

Rules:
- Temporary emotion is not a durable state change unless the text makes it consequential.
- Dialogue claims are not automatically facts.
- POV perception may be wrong or incomplete.
- A contradiction requires direct conflict with a known state.
- If a character plausibly changed state off-page and the text does not contradict causality, return warn or pass.
- Do not mark a character as violating state merely because wording differs.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="CharacterStatePromptAnalyzer",
    extra_required=["state_observations"],
)


class CharacterStatePromptAnalyzer(PromptJsonAnalyzer):
    name = "CharacterStatePromptAnalyzer"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA
