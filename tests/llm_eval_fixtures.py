"""Sample story fixtures for the llm_eval harness and context-pack tests.

The story text lives in ``tests/fixtures/llm_eval/*.json``; the runtime package
``forwin/llm_eval`` only loads such files and carries no story content of its own.
"""

from __future__ import annotations

from pathlib import Path

from forwin.llm_eval.cases import load_sample_contexts
from forwin.protocol.context import ChapterContextPack

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "llm_eval"
CONTEXTS_PATH = FIXTURE_DIR / "sample_contexts.json"
CASES_PATH = FIXTURE_DIR / "cases.json"


def sample_context(*, variant: int = 0) -> ChapterContextPack:
    contexts = load_sample_contexts(CONTEXTS_PATH)
    return contexts[variant % len(contexts)]
