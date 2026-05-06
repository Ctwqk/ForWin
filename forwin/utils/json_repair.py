from __future__ import annotations

import json
import re


class LLMJSONParseError(ValueError):
    def __init__(self, message: str, *, empty_response: bool = False) -> None:
        super().__init__(message)
        self.empty_response = empty_response


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
        if in_string and ord(char) < 0x20:
            control_replacements = {
                "\n": "\\n",
                "\r": "\\r",
                "\t": "\\t",
                "\b": "\\b",
                "\f": "\\f",
            }
            escaped.append(control_replacements.get(char, f"\\u{ord(char):04x}"))
            continue
        escaped.append(char)

    return "".join(escaped)


def _normalize_json_punctuation(text: str) -> str:
    return (
        text.replace("\ufeff", "")
        .replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )


def _extract_balanced_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        return ""

    stack: list[str] = []
    in_string = False
    escaped_next = False

    for index in range(start, len(text)):
        char = text[index]
        if escaped_next:
            escaped_next = False
            continue
        if char == "\\":
            escaped_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append(char)
            continue
        if char == "}":
            if stack and stack[-1] == "{":
                stack.pop()
            if not stack:
                return text[start : index + 1]
            continue
        if char == "]":
            if stack and stack[-1] == "[":
                stack.pop()
            continue
    return text[start:]


def _close_unbalanced_json(text: str) -> str:
    if not text:
        return text

    stack: list[str] = []
    in_string = False
    escaped_next = False

    for char in text:
        if escaped_next:
            escaped_next = False
            continue
        if char == "\\":
            escaped_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append(char)
            continue
        if char == "}" and stack and stack[-1] == "{":
            stack.pop()
            continue
        if char == "]" and stack and stack[-1] == "[":
            stack.pop()
            continue

    suffix: list[str] = []
    if in_string:
        suffix.append('"')
    while stack:
        opener = stack.pop()
        suffix.append("}" if opener == "{" else "]")
    return text + "".join(suffix)


def _try_parse_dict(text: str) -> dict | None:
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        return None
    return None


def _skip_json_ws_and_commas(text: str, index: int) -> int:
    while index < len(text) and (text[index].isspace() or text[index] == ","):
        index += 1
    return index


def _salvage_complete_top_level_arrays(text: str) -> dict | None:
    """Return completed top-level array items from a truncated object.

    Closing an unfinished JSON object can fabricate the last partial item. For
    LLM responses truncated by max_tokens, preserve only values that the JSON
    decoder can read completely from the original text.
    """
    source = text.strip()
    if not source.startswith("{"):
        return None

    decoder = json.JSONDecoder()
    result: dict[str, object] = {}
    index = 1
    saw_array = False

    while index < len(source):
        index = _skip_json_ws_and_commas(source, index)
        if index >= len(source) or source[index] == "}":
            break
        try:
            key, index = decoder.raw_decode(source, index)
        except json.JSONDecodeError:
            break
        if not isinstance(key, str):
            break
        index = _skip_json_ws_and_commas(source, index)
        if index >= len(source) or source[index] != ":":
            break
        index = _skip_json_ws_and_commas(source, index + 1)
        if index >= len(source):
            break

        if source[index] != "[":
            try:
                value, index = decoder.raw_decode(source, index)
            except json.JSONDecodeError:
                break
            result[key] = value
            continue

        saw_array = True
        items: list[object] = []
        index += 1
        array_complete = False
        while index < len(source):
            index = _skip_json_ws_and_commas(source, index)
            if index >= len(source):
                break
            if source[index] == "]":
                index += 1
                array_complete = True
                break
            try:
                item, next_index = decoder.raw_decode(source, index)
            except json.JSONDecodeError:
                break
            items.append(item)
            index = _skip_json_ws_and_commas(source, next_index)
            if index < len(source) and source[index] == "]":
                index += 1
                array_complete = True
                break
            if index < len(source) and source[index] == ",":
                index += 1

        if items or array_complete:
            result[key] = items
        if not array_complete:
            break

    if saw_array and result:
        return result
    return None


def _decode_jsonish_string(text: str) -> str:
    return (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
        .strip()
    )


def _extract_text_like_object(text: str) -> dict | None:
    candidates = [text]
    balanced = _extract_balanced_object(text)
    if balanced and balanced != text:
        candidates.append(balanced)

    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized:
            continue
        for field in ("body", "text"):
            match = re.search(
                rf'"{field}"\s*:\s*"(?P<content>.*)"\s*}}',
                normalized,
                flags=re.DOTALL,
            )
            if match is None:
                continue
            content = _decode_jsonish_string(match.group("content"))
            if not content:
                continue
            payload = {field: content}
            if field == "text":
                payload["body"] = content
            return payload
    return None


def parse_llm_json(raw: str, *, error_prefix: str = "LLM JSON parser") -> dict:
    original = strip_reasoning(raw)
    candidate = original.strip()
    if not candidate:
        raise LLMJSONParseError(
            f"{error_prefix}: empty response after stripping reasoning.",
            empty_response=True,
        )
    parsed = _try_parse_dict(candidate)
    if parsed is not None:
        return parsed

    code_block_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        original,
        flags=re.DOTALL,
    )
    if code_block_match:
        candidate = code_block_match.group(1).strip()
        parsed = _try_parse_dict(candidate)
        if parsed is not None:
            return parsed

    brace_candidate = _extract_balanced_object(original)
    if not brace_candidate:
        first_brace = original.find("{")
        if first_brace != -1:
            brace_candidate = original[first_brace:]

    repair_candidates: list[str] = []
    if brace_candidate:
        repair_candidates.extend(
            [
                brace_candidate.strip(),
                _close_unbalanced_json(brace_candidate.strip()),
            ]
        )

    if candidate != original:
        repair_candidates.extend(
            [
                candidate,
                _close_unbalanced_json(candidate),
            ]
        )
    normalized_original = _normalize_json_punctuation(original)
    if normalized_original != original:
        normalized_candidate = normalized_original.strip()
        normalized_brace_candidate = _extract_balanced_object(normalized_original)
        repair_candidates.extend(
            [
                normalized_candidate,
                _close_unbalanced_json(normalized_candidate),
                normalized_brace_candidate.strip() if normalized_brace_candidate else "",
                _close_unbalanced_json(normalized_brace_candidate.strip()) if normalized_brace_candidate else "",
            ]
        )

    seen: set[str] = set()
    for raw_candidate in repair_candidates:
        if not raw_candidate:
            continue
        for variant in (
            raw_candidate,
            _strip_trailing_commas(raw_candidate),
            _escape_control_chars_in_strings(_strip_trailing_commas(raw_candidate)),
        ):
            normalized = variant.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            parsed = _try_parse_dict(normalized)
            if parsed is not None:
                return parsed
            parsed = _salvage_complete_top_level_arrays(normalized)
            if parsed is not None:
                return parsed

    text_like = _extract_text_like_object(original)
    if text_like is None and normalized_original != original:
        text_like = _extract_text_like_object(normalized_original)
    if text_like is not None:
        return text_like

    snippet = original[:300].replace("\n", " ")
    raise LLMJSONParseError(
        f"{error_prefix}: could not extract valid JSON from LLM response. "
        f"First 300 chars: {snippet!r}"
    )
