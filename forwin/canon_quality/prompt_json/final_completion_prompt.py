from __future__ import annotations

from typing import Any

from .base import PromptJsonAnalyzer
from .schemas import common_output_schema

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Analyze whether the writer output sufficiently completes the intended chapter or scene.

Completion does not mean all story threads must be closed.
Evaluate only whether the required goals and beats for this output were satisfied.

Rules:
- A cliffhanger or hook can be valid if it matches planned_ending_type.
- Open threads are acceptable unless must_resolve_now is true.
- Do not fail merely because the ending introduces a new question.
- Separate structural incompletion from stylistic preference.
- If a required beat is missing, quote what is missing and explain.
- If the output appears intentionally open-ended, classify accordingly.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="FinalCompletionPromptAnalyzer",
    extra_required=["completion_assessment", "required_beat_status"],
)


class FinalCompletionPromptAnalyzer(PromptJsonAnalyzer):
    name = "FinalCompletionPromptAnalyzer"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA
