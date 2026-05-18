from __future__ import annotations

from forwin.canon_quality.chapter_review_form.replay_diff import compute_diff, normalize_countdown_row


def test_normalize_countdown_row_uses_downstream_fields_only() -> None:
    row = normalize_countdown_row(
        {
            "id": "volatile",
            "created_at": "ignored",
            "chapter_number": 3,
            "countdown_key": "main",
            "normalized_remaining_minutes": 59,
            "status": "active",
            "payload": {"evidence_quote": "主倒计时还有59分钟。"},
        }
    )

    assert row.key == "3:countdown:main"
    assert row.fields == {
        "normalized_remaining_minutes": 59,
        "status": "active",
        "evidence_quote": "主倒计时还有59分钟。",
    }


def test_compute_diff_classifies_add_remove_and_change() -> None:
    existing = [
        {
            "kind": "countdown",
            "chapter_number": 3,
            "subject": "main",
            "fields": {"status": "active", "normalized_remaining_minutes": 60},
        },
        {
            "kind": "character_state",
            "chapter_number": 3,
            "subject": "韩青",
            "fields": {"to_state": "alive"},
        },
    ]
    candidate = [
        {
            "kind": "countdown",
            "chapter_number": 3,
            "subject": "main",
            "fields": {"status": "active", "normalized_remaining_minutes": 59},
        },
        {
            "kind": "countdown",
            "chapter_number": 3,
            "subject": "branch",
            "fields": {"status": "active", "normalized_remaining_minutes": 10},
        },
    ]

    diff = compute_diff(existing_rows=existing, candidate_rows=candidate)

    assert {item.kind for item in diff} == {"change", "add", "remove"}
    changed = next(item for item in diff if item.kind == "change")
    assert changed.subject == "main"
    assert changed.before["normalized_remaining_minutes"] == 60
    assert changed.after["normalized_remaining_minutes"] == 59


def test_compute_diff_empty_when_logical_rows_match() -> None:
    row = {"kind": "countdown", "chapter_number": 3, "subject": "main", "fields": {"status": "active"}}

    assert compute_diff(existing_rows=[row], candidate_rows=[row]) == []
