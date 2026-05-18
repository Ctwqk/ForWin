from __future__ import annotations

from typing import Any

from .base import PromptJsonAnalyzer
from .schemas import common_output_schema

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Analyze whether the writer output contains identity consistency problems.

Identity consistency includes:
- canonical identity
- alias
- title
- disguise
- mistaken identity
- secret identity
- lineage or role identity
- who knows which identity

Rules:
- A character may be referred to by different names, aliases, titles, or pronouns if context supports it.
- A character's dialogue may contain lies or mistaken beliefs.
- A POV character may not know another character's true identity.
- Do not flag an identity issue merely because an alias is used.
- Flag only when the text itself asserts an identity that directly contradicts canon, or reveals knowledge a character should not have without support.
- If the issue is about reader clarity rather than canon contradiction, classify it as clarity_issue, not identity_contradiction.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="IdentityConsistencyPromptAnalyzer",
    extra_required=["identity_mentions"],
)


class IdentityConsistencyPromptAnalyzer(PromptJsonAnalyzer):
    name = "IdentityConsistencyPromptAnalyzer"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA
