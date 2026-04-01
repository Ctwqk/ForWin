from __future__ import annotations

import json
import re


def strip_reasoning(raw: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL).strip()


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _escape_control_chars_in_strings(text: str) -> str:
    escaped: list[str] = []
    in_string = False
    escaped_next = False

    for char in text:
        if escaped_next:
            escaped.append(char)
            escaped_next = False
            continue
        if char == "\\":
            escaped.append(char)
            escaped_next = True
            continue
        if char == '"':
            escaped.append(char)
            in_string = not in_string
            continue
        if in_string and char == "\n":
            escaped.append("\\n")
            continue
        if in_string and char == "\r":
            escaped.append("\\r")
            continue
        if in_string and char == "\t":
            escaped.append("\\t")
            continue
        escaped.append(char)

    return "".join(escaped)


def parse_llm_json(raw: str, *, error_prefix: str = "LLM JSON parser") -> dict:
    original = strip_reasoning(raw)
    candidate = original.strip()
    try:
        result = json.loads(candidate)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    code_block_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        original,
        flags=re.DOTALL,
    )
    if code_block_match:
        candidate = code_block_match.group(1).strip()
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    first_brace = original.find("{")
    last_brace = original.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = original[first_brace : last_brace + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        cleaned = _strip_trailing_commas(candidate)
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        repaired = _escape_control_chars_in_strings(cleaned)
        try:
            result = json.loads(repaired)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    snippet = original[:300].replace("\n", " ")
    raise ValueError(
        f"{error_prefix}: could not extract valid JSON from LLM response. "
        f"First 300 chars: {snippet!r}"
    )
