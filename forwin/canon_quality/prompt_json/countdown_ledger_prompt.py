from __future__ import annotations

from typing import Any

from .base import PromptJsonAnalyzer
from .schemas import common_output_schema

PROMPT_VERSION = "1.0"

USER_PROMPT_TEMPLATE = """Analyze whether the writer output changes, contradicts, fulfills, or ambiguously references any countdown, deadline, timer, scheduled event, or temporal obligation.

Classify each relevant temporal item as one of:
- unchanged
- advanced
- fulfilled
- contradicted
- newly_introduced
- ambiguous_reference
- no_relevant_countdown

Important rules:
- Do not treat every number or time phrase as a countdown.
- Dialogue may be mistaken, deceptive, metaphorical, or approximate.
- If the text says "soon", "later", "after a while", or another vague phrase, do not create a precise ledger change.
- If elapsed time is not explicit, do not infer it.
- If a deadline appears fulfilled, quote the exact text showing fulfillment.
- If a contradiction is possible but not explicit, return warn or uncertain, not fail.
"""

OUTPUT_SCHEMA: dict[str, Any] = common_output_schema(
    analyzer="CountdownLedgerPromptAnalyzer",
    extra_required=["countdown_updates"],
)


class CountdownLedgerPromptAnalyzer(PromptJsonAnalyzer):
    name = "CountdownLedgerPromptAnalyzer"
    version = "1.0"
    prompt_version = PROMPT_VERSION
    user_prompt_template = USER_PROMPT_TEMPLATE
    output_schema = OUTPUT_SCHEMA
