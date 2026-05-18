from __future__ import annotations

from forwin.writer.prompt_budget import (
    prompt_budget_warning,
    prompt_message_chars,
    prompt_revision_hash,
)


def test_prompt_budget_counts_roles_and_content() -> None:
    messages = [
        {"role": "system", "content": "abc"},
        {"role": "user", "content": "正文"},
    ]

    assert prompt_message_chars(messages) == len("systemabcuser正文")


def test_prompt_budget_warning_marks_over_budget_only_when_needed() -> None:
    messages = [{"role": "user", "content": "12345"}]

    assert prompt_budget_warning(messages, max_chars=10) == {
        "char_count": 9,
        "max_chars": 10,
        "over_budget": False,
    }
    assert prompt_budget_warning(messages, max_chars=8) == {
        "char_count": 9,
        "max_chars": 8,
        "over_budget": True,
    }


def test_prompt_revision_hash_is_stable_and_content_sensitive() -> None:
    messages = [{"role": "user", "content": "same"}]

    assert prompt_revision_hash(messages) == prompt_revision_hash([dict(messages[0])])
    assert prompt_revision_hash(messages) != prompt_revision_hash(
        [{"role": "user", "content": "changed"}]
    )
