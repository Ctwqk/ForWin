from __future__ import annotations

from forwin.canon_quality.artifact_ledger import analyze_artifact_counts, parse_chinese_number


def test_chinese_number_and_ranges_parse() -> None:
    assert parse_chinese_number("五十九") == 59
    assert parse_chinese_number("六十一") == 61


def test_conflicting_artifact_totals_are_error() -> None:
    signals, ledgers = analyze_artifact_counts(
        project_id="p1",
        chapter_number=57,
        draft_id="d1",
        body="档案柜里写着六十份档案，背面却标成六十一份。",
        previous_ledgers=[],
        target_total=60,
    )

    assert ledgers
    assert any(signal.signal_type == "artifact_count_conflict" and signal.severity == "error" for signal in signals)


def test_artifact_counts_are_disabled_without_known_collection_target() -> None:
    signals, ledgers = analyze_artifact_counts(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="陆明看到47条记录被抹除，只剩三条档案索引和第五份待修复旧档。",
        previous_ledgers=[],
        target_total=0,
    )

    assert signals == []
    assert ledgers == []


def test_artifact_range_advances_count() -> None:
    signals, ledgers = analyze_artifact_counts(
        project_id="p1",
        chapter_number=15,
        draft_id="d1",
        body="他们同时取回第15-18份档案。",
        previous_ledgers=[],
        target_total=60,
    )

    assert not [signal for signal in signals if signal.severity == "error"]
    assert ledgers[0].new_items == ["15", "16", "17", "18"]
