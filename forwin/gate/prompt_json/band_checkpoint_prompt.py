from __future__ import annotations

from typing import Any

from forwin.canon_quality.prompt_json.base import PromptJsonAnalyzer
from forwin.canon_quality.prompt_json.schemas import common_output_schema

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Evaluate whether the writer output remains within the intended narrative band.

Rules:
- Minor expansion is acceptable if it supports the current scene.
- Do not fail because the output is more detailed than expected.
- A band violation occurs only when the output materially jumps timeline, reveals locked information early, resolves future beats prematurely, or changes the intended scope.
- If the output introduces a useful but unplanned detail, classify as needs_plan_update, not fail.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="BandCheckpointPromptEvaluator",
    extra_required=["band_assessment"],
)


class BandCheckpointPromptEvaluator(PromptJsonAnalyzer):
    name = "BandCheckpointPromptEvaluator"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA
