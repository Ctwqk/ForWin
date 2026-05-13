from __future__ import annotations

from forwin.canon_quality.style import analyze_style_telemetry


def test_repeated_style_motif_emits_warning_telemetry() -> None:
    signals, telemetry = analyze_style_telemetry(
        project_id="p1",
        chapter_number=10,
        draft_id="d1",
        body="铁锈味在走廊里翻涌。冷白光照着通风管道。你疯了。铁锈味再次涌上来。",
        previous_metrics=[
            {"style_motifs": ["铁锈味", "冷白光", "你疯了"]},
            {"style_motifs": ["铁锈味", "通风管道"]},
        ],
    )

    assert "铁锈味" in telemetry.top_repeated_motifs
    assert any(signal.signal_type == "style_repetition" and signal.severity == "warning" for signal in signals)
