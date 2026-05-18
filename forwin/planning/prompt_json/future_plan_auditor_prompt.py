from __future__ import annotations

from typing import Any

from forwin.canon_quality.prompt_json.base import PromptJsonAnalyzer
from forwin.canon_quality.prompt_json.schemas import common_output_schema

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Analyze whether the writer output creates problems for the future plan.

Evaluate:
1. Does the output contradict required future plan beats?
2. Does the output make a planned event impossible?
3. Does the output require updating the future plan?
4. Is the apparent conflict acceptable because the plan can flex?

Rules:
- Future plans are not canon unless explicitly marked locked.
- Do not fail if the future plan can be adjusted without breaking canon.
- A locked future beat contradiction may be blocking.
- A soft plan mismatch should be warn, not fail.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="FuturePlanPromptAuditor",
    extra_required=["plan_impacts"],
)


class FuturePlanPromptAuditor(PromptJsonAnalyzer):
    name = "FuturePlanPromptAuditor"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA
