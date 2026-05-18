from __future__ import annotations

from typing import Any

from forwin.canon_quality.prompt_json.base import PromptJsonAnalyzer
from forwin.canon_quality.prompt_json.schemas import common_output_schema

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Validate whether the proposed plan patch is safe and consistent.

Evaluate:
1. Does the patch preserve canon?
2. Does it modify only allowed plan fields?
3. Does it explain why the change is needed?
4. Does it weaken or delete locked constraints?
5. Does it resolve the issue it claims to resolve?

Rules:
- Plan patches may change soft future plans.
- Plan patches must not rewrite established canon.
- Deleting a locked constraint is critical unless explicitly authorized.
- If the patch is underspecified but not dangerous, return warn.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="PlanPatchPromptValidator",
    extra_required=["patch_assessment", "field_changes"],
)


class PlanPatchPromptValidator(PromptJsonAnalyzer):
    name = "PlanPatchPromptValidator"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA
