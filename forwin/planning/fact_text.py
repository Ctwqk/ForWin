"""Whether planner text states a fact's proposition: the one comparison every writer-facing plan field goes through."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def normalized_text(text: str) -> str:
    """The text without any whitespace, so an inserted space or line break never hides a proposition."""
    return "".join(text.split())


def states_proposition(text: str, proposition: str) -> bool:
    """Whether ``text`` contains ``proposition``, whitespace aside; an empty proposition is never stated."""
    needle = normalized_text(proposition)
    return bool(needle) and needle in normalized_text(text)


def stated_facts(texts: Iterable[str], propositions: Mapping[str, str]) -> list[str]:
    """The refs of ``propositions`` (ref -> proposition) that any of ``texts`` states, in ref order."""
    joined = [normalized_text(text) for text in texts]
    return [ref for ref, proposition in sorted(propositions.items())
            if (needle := normalized_text(proposition)) and any(needle in text for text in joined)]


__all__ = ["normalized_text", "stated_facts", "states_proposition"]
