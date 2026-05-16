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
GENERIC_IDENTITY_NAMES = {
    "照片",
    "档案",
    "终端",
    "屏幕",
    "系统",
    "文件",
    "文件夹",
    "记录",
    "编号",
    "名字",
    "纸条",
    "钥匙",
}


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
        if _is_generic_identity_name(name):
            continue
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
    names = {
        str(name)
        for name in central_characters
        if str(name).strip() and not _is_generic_identity_name(str(name))
    }
    for raw in facts:
        item = raw.model_dump(mode="json") if isinstance(raw, IdentityRoleFact) else dict(raw)
        name = str(item.get("character_name") or "").strip()
        if name and not _is_generic_identity_name(name):
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
        after = _same_sentence_after_name(text, match.end(), limit=80)
        first_she = after.find("她")
        first_he = after.find("他")
        if first_she >= 0 and _looks_like_object_pronoun(after, first_she):
            first_she = -1
        if first_he >= 0 and _looks_like_object_pronoun(after, first_he):
            first_he = -1
        if first_she >= 0 and (first_he < 0 or first_she < first_he):
            return "female", (match.start(), min(len(text), match.end() + len(after)))
        if first_he >= 0 and (first_she < 0 or first_he < first_she):
            return "male", (match.start(), min(len(text), match.end() + len(after)))
        male_score = sum(_marker_weight(marker) for marker in MALE_MARKERS if marker not in {"他", "她"} and marker in context)
        female_score = sum(_marker_weight(marker) for marker in FEMALE_MARKERS if marker not in {"他", "她"} and marker in context)
        if male_score > female_score:
            return "male", (match.start(), min(len(text), match.end() + 80))
        if female_score > male_score:
            return "female", (match.start(), min(len(text), match.end() + 80))
    return "", (0, 0)


def _looks_like_object_pronoun(after_name: str, pronoun_index: int) -> bool:
    tail = str(after_name or "")[pronoun_index + 1 : pronoun_index + 24]
    if pronoun_index <= 1 and tail.startswith(
        (
            "默念",
            "在心里默念",
            "心里默念",
            "在心里念着",
            "心里念着",
            "念着",
            "念出",
            "想起",
            "回忆",
            "记住",
            "输入",
            "搜索",
            "查找",
            "调出",
        )
    ) and any(marker in tail for marker in ("名字", "姓名", "名号", "这个名")):
        return True
    before = str(after_name or "")[max(0, pronoun_index - 8) : pronoun_index]
    before_candidates = (before, before.rstrip("了过"))
    after = str(after_name or "")[pronoun_index + 1 : pronoun_index + 5]
    if before.rstrip().endswith("比") and after.startswith(("早", "晚", "快", "慢", "先", "后")):
        return True
    if before.rstrip().endswith("把"):
        return True
    if any(
        before.rstrip().endswith(marker)
        for marker in (
            "直视",
            "直视着",
            "凝视",
            "凝视着",
            "注视",
            "注视着",
            "抬头看",
            "低头看",
            "回头看",
            "转头看",
            "抬眼看",
            "看了看",
            "看见",
            "看到",
            "瞥见",
            "望见",
        )
    ):
        return True
    if after.startswith("们"):
        return True
    if before.rstrip().endswith(
        (
            "被抓了，",
            "被捕了，",
            "落网了，",
            "失踪了，",
            "失联了，",
            "被带走了，",
            "被关押，",
            "被关押了，",
        )
    ):
        return True
    if any(marker in before for marker in ("然后", "随后", "接着", "紧接着")):
        return True
    return any(candidate.endswith(
        (
            "递到",
            "递给",
            "给",
            "给过",
            "给了",
            "丢给",
            "交到",
            "交给",
            "送到",
            "送给",
            "拿到",
            "拿给",
            "塞到",
            "塞给",
            "推到",
            "推给",
            "推了",
            "推开",
            "递向",
            "交向",
            "展示给",
            "察觉到",
            "注意到",
            "发现",
            "告诉",
            "对",
            "不让",
            "让",
            "保护",
            "掩护",
            "救",
            "陪",
            "陪着",
            "帮",
            "帮着",
            "跟着",
            "随着",
            "顺着",
            "带",
            "架着",
            "架住",
            "扶着",
            "扶住",
            "拖着",
            "拖住",
            "按在",
            "搭在",
            "蹲在",
            "站在",
            "坐在",
            "靠在",
            "守在",
            "挡在",
            "停在",
            "留在",
            "跪在",
            "半跪在",
            "背对着",
            "面对着",
            "朝向",
            "看了",
            "回头看了",
            "看向",
            "盯着",
            "看着",
            "望着",
            "瞪着",
            "抓住",
            "握住",
            "扶住",
            "拉住",
            "拽住",
            "按住",
            "打断",
            "截断",
            "制止",
            "拦住",
            "忘记",
            "记起",
            "想起",
        )
    ) for candidate in before_candidates) or after.startswith(
        (
            "面前",
            "手里",
            "怀里",
            "身边",
            "身侧",
            "身后",
            "身前",
            "旁边",
            "背后",
            "手腕",
            "手",
            "手指",
            "肩",
            "肩膀",
            "胳膊",
            "手臂",
            "衣领",
            "后背",
            "胸口",
            "的手腕",
            "的手",
            "的手指",
            "的肩",
            "的肩膀",
            "的胳膊",
            "的手臂",
            "的衣领",
            "的后背",
            "的胸口",
        )
    )


def _same_sentence_after_name(text: str, start: int, *, limit: int) -> str:
    window = str(text or "")[start : start + max(0, int(limit or 0))]
    window = window.lstrip("。！？!? \t\r\n")
    if window.startswith(("”", "」", "』", "'")):
        return ""
    stops = [
        idx
        for marker in ("。", "！", "？", "；", ";", "：", ":", "——", "—", "\n", "“", "”", "「", "」", "\"")
        if (idx := window.find(marker)) >= 0
    ]
    if stops:
        return window[: min(stops)]
    return window


def _clean_relation_name(name: str) -> str:
    result = str(name or "").strip("，,。；;：:、 ")
    for prefix in ("得知", "知道", "确认", "发现", "原来", "其实"):
        if result.startswith(prefix):
            result = result[len(prefix) :]
    if result.startswith(("知", "认", "现")) and len(result) >= 3:
        result = result[1:]
    if result in {"自己", "他的", "她的", "父亲", "母亲"} or _is_generic_identity_name(result):
        return ""
    return result


def _is_generic_identity_name(name: str) -> bool:
    return str(name or "").strip() in GENERIC_IDENTITY_NAMES


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
