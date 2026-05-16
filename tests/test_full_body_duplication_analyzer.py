from __future__ import annotations

from forwin.canon_quality.duplication import analyze_full_body_duplication


def test_repeated_paragraph_blocks_canon() -> None:
    paragraph = "陆明沿着通风管道向前，警报声在身后追来。"
    body = f"{paragraph}\n\n他按住密钥继续逃。\n\n{paragraph}"

    signals, metrics = analyze_full_body_duplication(
        project_id="p1",
        chapter_number=8,
        draft_id="d1",
        body=body,
    )

    assert metrics.duplicate_spans
    assert any(signal.signal_type == "body_duplicate_span" and signal.severity == "error" for signal in signals)


def test_short_intentional_callback_is_warning_not_error() -> None:
    body = "别回头。\n\n陆明继续向前。\n\n别回头。"

    signals, _metrics = analyze_full_body_duplication(
        project_id="p1",
        chapter_number=8,
        draft_id="d1",
        body=body,
    )

    assert not [signal for signal in signals if signal.severity == "error"]
