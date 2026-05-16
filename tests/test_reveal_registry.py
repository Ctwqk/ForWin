from __future__ import annotations

from forwin.canon_quality.reveal_registry import analyze_reveals


def test_repeated_reveal_packaged_as_new_is_error() -> None:
    signals, entries = analyze_reveals(
        project_id="p1",
        chapter_number=7,
        draft_id="d1",
        reveal_claims=["核心系统存在后门"],
        previous_entries=[
            {
                "reveal_key": "核心系统存在后门",
                "claim_summary": "核心系统存在后门",
                "first_revealed_chapter": 6,
                "repeat_count": 0,
                "status": "new",
            }
        ],
        body="陆明第一次发现：核心系统存在后门。",
    )

    assert entries[0].repeat_count == 1
    assert any(signal.signal_type == "repeated_reveal_as_new" and signal.severity == "error" for signal in signals)


def test_repeated_reveal_with_escalation_is_not_error() -> None:
    signals, entries = analyze_reveals(
        project_id="p1",
        chapter_number=7,
        draft_id="d1",
        reveal_claims=["核心系统存在后门"],
        previous_entries=[
            {
                "reveal_key": "核心系统存在后门",
                "claim_summary": "核心系统存在后门",
                "first_revealed_chapter": 6,
                "repeat_count": 0,
                "status": "new",
            }
        ],
        body="核心系统存在后门，这次新增证据指向陆明父亲留下的密钥。",
    )

    assert entries[0].status == "escalated"
    assert not [signal for signal in signals if signal.severity == "error"]
