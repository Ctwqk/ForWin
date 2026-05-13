from __future__ import annotations

import re
from typing import Any

from .signals import CanonQualitySignal, IdentityRoleFact, make_signal_id

RELATIONSHIPS = ("父亲", "祖父", "曾祖父", "母亲", "祖母", "姐姐", "哥哥", "妹妹", "弟弟")
BRIDGE_MARKERS = ("伪装", "误导", "此前", "其实", "真相", "身份是假的", "身份是伪装", "谎言")


def analyze_identity_roles(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
    previous_facts: list[dict[str, Any] | IdentityRoleFact] | None = None,
    central_characters: set[str] | None = None,
) -> tuple[list[CanonQualitySignal], list[IdentityRoleFact]]:
    text = str(body or "")
    signals: list[CanonQualitySignal] = []
    facts: list[IdentityRoleFact] = []
    pattern = re.compile(
        r"([\u4e00-\u9fff]{2,4}?)(?:其实|真正|终于确认|确认)?是他的("
        + "|".join(RELATIONSHIPS)
        + r")"
    )
    for match in pattern.finditer(text):
        name = match.group(1)
        relationship = match.group(2)
        fact = IdentityRoleFact(
            project_id=project_id,
            character_name=name,
            relationship_to_protagonist=relationship,
            temporal_valid_from=chapter_number,
            evidence_refs=[f"body:{match.start()}-{match.end()}"],
            payload={"draft_id": draft_id},
        )
        facts.append(fact)
        previous = _previous_fact_for(name, previous_facts or [])
        if previous is None:
            continue
        previous_relationship = str(previous.get("relationship_to_protagonist") or "")
        if previous_relationship and previous_relationship != relationship:
            subject = f"identity:{name}:relationship"
            has_bridge = any(marker in text for marker in BRIDGE_MARKERS)
            signals.append(
                CanonQualitySignal(
                    signal_id=make_signal_id(
                        project_id,
                        chapter_number,
                        "identity_relationship_bridge" if has_bridge else "identity_relationship_conflict",
                        subject,
                    ),
                    project_id=project_id,
                    chapter_number=chapter_number,
                    signal_type="identity_relationship_bridge" if has_bridge else "identity_relationship_conflict",
                    severity="warning" if has_bridge else "error",
                    target_scope="character",
                    subject_key=subject,
                    description=(
                        f"{name} 与主角关系从「{previous_relationship}」变为「{relationship}」"
                        + ("，正文提供了身份桥接。" if has_bridge else "，但没有明确身份桥接。")
                    ),
                    evidence_refs=[*fact.evidence_refs, f"chapter:{previous.get('chapter_number', 0)}"],
                    span_start=match.start(),
                    span_end=match.end(),
                    payload={"draft_id": draft_id, "previous_fact": previous, "central": name in (central_characters or set())},
                )
            )
    return signals, facts


def _previous_fact_for(name: str, facts: list[dict[str, Any] | IdentityRoleFact]) -> dict[str, Any] | None:
    for raw in reversed(facts):
        item = raw.model_dump(mode="json") if isinstance(raw, IdentityRoleFact) else dict(raw)
        if str(item.get("character_name") or "") == name:
            return item
    return None
