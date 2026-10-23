"""Writer-facing templates of the POV world view; story content comes only from the view's fields."""

from __future__ import annotations

from collections.abc import Sequence


# A forbidden topic names an unrevealed secret's subjects, never its proposition.
FORBIDDEN_TOPIC_TEMPLATE = "涉及{subjects}的未揭示真相"
FORBIDDEN_TOPIC_SUBJECT_SEPARATOR = "、"


def forbidden_topic_text(subjects: Sequence[str]) -> str:
    """The topic of a secret the writer must not settle, built from its subjects' names."""
    return FORBIDDEN_TOPIC_TEMPLATE.format(subjects=FORBIDDEN_TOPIC_SUBJECT_SEPARATOR.join(subjects))


__all__ = ["FORBIDDEN_TOPIC_SUBJECT_SEPARATOR", "FORBIDDEN_TOPIC_TEMPLATE", "forbidden_topic_text"]
