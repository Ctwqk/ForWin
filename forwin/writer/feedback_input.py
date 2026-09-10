"""Evidence of the exact marked hints passed to the Writer model adapter."""

from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from forwin.protocol.context import AudienceHintView

_MARKER = re.compile(
    r"\[feedback_action:([a-zA-Z0-9_-]{1,64}):([a-f0-9]{64})\](.*?)\[/feedback_action\]",
    re.DOTALL,
)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_feedback_hints(hints: AudienceHintView) -> str | None:
    items = hints.clipped().items
    if not items:
        return None
    lines = ["【读者信号提示（仅供参考，标记不得写入正文）】"]
    for item in items:
        lines.append(
            f"[feedback_action:{item.action_id}:{text_sha256(item.text)}]{item.text}[/feedback_action]"
        )
    return "\n".join(lines)


def feedback_input_evidence(messages: list[dict], stage_key: str) -> dict | None:
    hints = []
    seen = set()
    for message in messages:
        for match in _MARKER.finditer(str(message.get("content", ""))):
            action_id, digest, text = match.groups()
            if action_id not in seen and text_sha256(text) == digest:
                hints.append({"action_id": action_id, "hint_sha256": digest})
                seen.add(action_id)
    if not hints:
        return None
    return {
        "input_id": uuid4().hex,
        "input_status": "attempted_input",
        "adapter_outcome": "unknown",
        "messages_sha256": text_sha256(
            json.dumps(
                messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        ),
        "stage_key": stage_key,
        "hints": hints,
    }
