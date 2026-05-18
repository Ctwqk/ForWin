from __future__ import annotations

from typing import Any

from .base import PromptJsonAnalyzer
from .schemas import common_output_schema

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Analyze whether the writer output satisfies, violates, defers, or ignores active obligations.

Obligations may include:
- promises
- tasks
- debts
- explicit vows
- external requirements
- plan-required beats
- canon-required followups

Rules:
- Do not require an obligation to be resolved unless it is marked due_now or must_address_in_current_output.
- Mentioning an obligation does not necessarily fulfill it.
- A character can intentionally fail an obligation; distinguish narrative failure from writing failure.
- If the output defers an obligation in a plausible way, classify as deferred, not failed.
- If an obligation is absent but not due now, do not flag it as a blocking issue.
- Require direct evidence for fulfilled or failed status.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="ObligationVerifierPromptAnalyzer",
    extra_required=["obligation_status"],
)


class ObligationVerifierPromptAnalyzer(PromptJsonAnalyzer):
    name = "ObligationVerifierPromptAnalyzer"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA
