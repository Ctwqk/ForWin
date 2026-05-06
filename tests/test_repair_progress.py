from __future__ import annotations

import inspect

from forwin.orchestrator.loop import WritingOrchestrator


def test_review_repair_loop_emits_distinct_progress_stages() -> None:
    source = inspect.getsource(WritingOrchestrator._review_and_maybe_rewrite)

    assert 'stage="repairing_chapter"' in source
    assert 'stage="repair_review"' in source
