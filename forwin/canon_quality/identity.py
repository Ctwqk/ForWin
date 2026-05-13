from __future__ import annotations

import re
from typing import Any

from .signals import CanonQualitySignal, IdentityRoleFact, make_signal_id

RELATIONSHIPS = ("父亲", "祖父", "曾祖父", "母亲", "祖母", "姐姐", "哥哥", "妹妹", "弟弟", "叔叔", "舅舅", "姑姑", "阿姨")
BRIDGE_MARKERS = ("伪装", "误导", "此前", "其实", "真相", "身份是假的", "身份是伪装", "谎言")
MALE_RELATIONSHIPS = {"父亲", "祖父", "曾祖父", "哥哥", "弟弟", "叔叔", "舅舅"}
FEMALE_RELATIONSHIPS = {"母亲", "祖母", "姐姐", "妹妹", "姑姑", "阿姨"}
MALE_MARKERS = ("男人", "男子", "叔叔", "舅舅", "父亲的弟弟", "他")
FEMALE_MARKERS = ("女人", "女子", "姑娘", "姐姐", "妹妹", "她")


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
    facts = extract_identity_role_facts(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        text=text,
        known_names=_known_names(previous_facts or [], central_characters or set()),
    )
    for fact in facts:
        name = fact.character_name
        relationship = fact.relationship_to_protagonist
        previous = _previous_fact_for(name, previous_facts or [])
        if previous is not None:
            previous_relationship = str(previous.get("relationship_to_protagonist") or "")
        else:
            previous_relationship = ""
        if previous_relationship and relationship and previous_relationship != relationship:
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
                    span_start=_span_start(fact),
                    span_end=_span_end(fact),
                    payload={"draft_id": draft_id, "previous_fact": previous, "central": name in (central_characters or set())},
                )
            )
        previous_gender = _previous_gender_for(name, previous_facts or [])
        current_gender = str(fact.payload.get("gender_label") or "")
        if previous_gender and current_gender and previous_gender != current_gender:
            subject = f"identity:{name}:gender"
            signals.append(
                CanonQualitySignal(
                    signal_id=make_signal_id(project_id, chapter_number, "identity_gender_conflict", subject),
                    project_id=project_id,
                    chapter_number=chapter_number,
                    signal_type="identity_gender_conflict",
                    severity="error",
                    target_scope="character",
                    subject_key=subject,
                    description=f"{name} 的性别/代词标记从「{previous_gender}」变为「{current_gender}」，但没有明确身份桥接。",
                    evidence_refs=[*fact.evidence_refs],
                    span_start=_span_start(fact),
                    span_end=_span_end(fact),
                    payload={"draft_id": draft_id, "previous_gender": previous_gender, "current_gender": current_gender},
                )
            )
    return signals, facts


def extract_identity_role_facts(
    *,
    project_id: str,
    chapter_number: int,
    text: str,
    draft_id: str = "",
    known_names: set[str] | None = None,
) -> list[IdentityRoleFact]:
    content = str(text or "")
    facts: list[IdentityRoleFact] = []
    seen: set[tuple[str, str, str]] = set()
    pattern = re.compile(
        r"([\u4e00-\u9fff]{2,4}?)(?:其实|真正|终于确认|确认|原来)?(?:是|就是)(?:他的|自己)?("
        + "|".join(RELATIONSHIPS)
        + r")"
    )
    for match in pattern.finditer(content):
        name = _clean_relation_name(match.group(1))
        relationship = match.group(2)
        if not name:
            continue
        gender = _gender_for_relationship(relationship)
        key = (name, relationship, gender)
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            IdentityRoleFact(
                project_id=project_id,
                character_name=name,
                relationship_to_protagonist=relationship,
                temporal_valid_from=chapter_number,
                evidence_refs=[f"body:{match.start()}-{match.end()}"],
                payload={"draft_id": draft_id, "gender_label": gender, "span_start": match.start(), "span_end": match.end()},
            )
        )
    for name in sorted(known_names or set()):
        gender, span = _gender_marker_for_name(content, name)
        if not gender:
            continue
        key = (name, "", gender)
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            IdentityRoleFact(
                project_id=project_id,
                character_name=name,
                role_label=f"gender:{gender}",
                temporal_valid_from=chapter_number,
                evidence_refs=[f"body:{span[0]}-{span[1]}"],
                payload={"draft_id": draft_id, "gender_label": gender, "span_start": span[0], "span_end": span[1]},
            )
        )
    return facts


def _previous_fact_for(name: str, facts: list[dict[str, Any] | IdentityRoleFact]) -> dict[str, Any] | None:
    for raw in reversed(facts):
        item = raw.model_dump(mode="json") if isinstance(raw, IdentityRoleFact) else dict(raw)
        if str(item.get("character_name") or "") == name:
            return item
    return None


def _previous_gender_for(name: str, facts: list[dict[str, Any] | IdentityRoleFact]) -> str:
    for raw in reversed(facts):
        item = raw.model_dump(mode="json") if isinstance(raw, IdentityRoleFact) else dict(raw)
        if str(item.get("character_name") or "") != name:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        gender = str(payload.get("gender_label") or "")
        if gender:
            return gender
    return ""


def _known_names(facts: list[dict[str, Any] | IdentityRoleFact], central_characters: set[str]) -> set[str]:
    names = {str(name) for name in central_characters if str(name).strip()}
    for raw in facts:
        item = raw.model_dump(mode="json") if isinstance(raw, IdentityRoleFact) else dict(raw)
        name = str(item.get("character_name") or "").strip()
        if name:
            names.add(name)
    return names


def _gender_for_relationship(relationship: str) -> str:
    if relationship in MALE_RELATIONSHIPS:
        return "male"
    if relationship in FEMALE_RELATIONSHIPS:
        return "female"
    return ""


def _gender_marker_for_name(text: str, name: str) -> tuple[str, tuple[int, int]]:
    for match in re.finditer(re.escape(name), text):
        start = max(0, match.start() - 40)
        end = min(len(text), match.end() + 80)
        context = text[start:end]
        after = text[match.end() : end]
        first_she = after.find("她")
        first_he = after.find("他")
        if first_she >= 0 and (first_he < 0 or first_she < first_he):
            return "female", (match.start(), end)
        if first_he >= 0 and (first_she < 0 or first_he < first_she):
            return "male", (match.start(), end)
        male_score = sum(_marker_weight(marker) for marker in MALE_MARKERS if marker in context)
        female_score = sum(_marker_weight(marker) for marker in FEMALE_MARKERS if marker in context)
        if male_score > female_score:
            return "male", (match.start(), min(len(text), match.end() + 80))
        if female_score > male_score:
            return "female", (match.start(), min(len(text), match.end() + 80))
    return "", (0, 0)


def _clean_relation_name(name: str) -> str:
    result = str(name or "").strip("，,。；;：:、 ")
    for prefix in ("得知", "知道", "确认", "发现", "原来", "其实"):
        if result.startswith(prefix):
            result = result[len(prefix) :]
    if result.startswith(("知", "认", "现")) and len(result) >= 3:
        result = result[1:]
    if result in {"自己", "他的", "她的", "父亲", "母亲"}:
        return ""
    return result


def _marker_weight(marker: str) -> int:
    if marker in {"他", "她"}:
        return 2
    return 3


def _span_start(fact: IdentityRoleFact) -> int | None:
    value = fact.payload.get("span_start")
    return int(value) if isinstance(value, int) else None


def _span_end(fact: IdentityRoleFact) -> int | None:
    value = fact.payload.get("span_end")
    return int(value) if isinstance(value, int) else None
