from __future__ import annotations

import re

from .signals import CanonQualitySignal, make_signal_id


BLOCKED_BODY_PLACEHOLDERS = ("一名相关人员", "相关人员")
BLOCKED_INTERNAL_STATE_KEYS = (
    "memory_reset",
    "archive_cleanup",
    "terminal_audit_window",
    "core_access_window",
    "public_countdown",
    "countdown_key",
)
BARE_ROLE_PLACEHOLDERS = ("工作人员",)
PROTAGONIST_PLACEHOLDER_ROLES = (
    "工作人员",
    "主角",
    "主人公",
)


def analyze_placeholder_leakage(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
    summary: str = "",
    expected_character_names: set[str] | None = None,
) -> list[CanonQualitySignal]:
    signals: list[CanonQualitySignal] = []
    text = str(body or "")
    for token in BLOCKED_INTERNAL_STATE_KEYS:
        start = text.find(token)
        if start < 0:
            continue
        subject = f"internal_state_key:{token}"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "internal_state_key_leakage", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="internal_state_key_leakage",
                severity="error",
                target_scope="body",
                subject_key=subject,
                description=f"章节正文泄漏内部状态键「{token}」，不能进入 canon。",
                evidence_refs=[f"body:{start}-{start + len(token)}"],
                span_start=start,
                span_end=start + len(token),
                payload={"draft_id": draft_id, "internal_state_key": token},
            )
        )
        return signals
    seen: set[str] = set()
    for placeholder in BLOCKED_BODY_PLACEHOLDERS:
        start = text.find(placeholder)
        if start < 0 or placeholder in seen:
            continue
        seen.add(placeholder)
        subject = f"placeholder:{placeholder}"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "placeholder_leakage", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="placeholder_leakage",
                severity="error",
                target_scope="body",
                subject_key=subject,
                description=f"章节正文包含占位符「{placeholder}」，不能进入 canon。",
                evidence_refs=[f"body:{start}-{start + len(placeholder)}"],
                span_start=start,
                span_end=start + len(placeholder),
                payload={"draft_id": draft_id, "placeholder": placeholder},
            )
        )
    if signals:
        return signals

    protagonist_signal = _analyze_protagonist_placeholder(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=text,
        summary=str(summary or ""),
        expected_character_names=expected_character_names or set(),
    )
    if protagonist_signal is not None:
        signals.append(protagonist_signal)
        return signals

    bare_role_signal = _analyze_bare_role_placeholder(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=text,
    )
    if bare_role_signal is not None:
        signals.append(bare_role_signal)
        return signals

    summary_text = str(summary or "")
    for placeholder in BLOCKED_BODY_PLACEHOLDERS:
        start = summary_text.find(placeholder)
        if start < 0:
            continue
        subject = f"placeholder:{placeholder}"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "placeholder_leakage", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="placeholder_leakage",
                severity="warning",
                target_scope="chapter",
                subject_key=subject,
                description=f"章节 summary 包含待确认占位符「{placeholder}」。",
                evidence_refs=[f"summary:{start}-{start + len(placeholder)}"],
                payload={"draft_id": draft_id, "placeholder": placeholder},
            )
        )
        break
    return signals


def _analyze_bare_role_placeholder(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    body: str,
) -> CanonQualitySignal | None:
    for placeholder in BARE_ROLE_PLACEHOLDERS:
        standalone = re.search(
            rf"(^|[。！？\n\r])\s*({re.escape(placeholder)})\s*[。！？]?(?=$|[。！？\n\r])",
            body,
        )
        label_context = re.search(
            rf"(签名|署名|徽章|证件|标记|权限|名字|姓名)[^。！？\n\r]{{0,20}}({re.escape(placeholder)})",
            body,
        )
        actor_context = re.search(
            rf"({re.escape(placeholder)})(?:停下|走近|抬头|伸手|看|说|问|冷笑|追|拦|取|的声音)",
            body,
        )
        role_as_name = re.search(
            rf"(队长|巡检员|操作员|守卫)[，,、：:\s]*({re.escape(placeholder)})(?=[。”，,、\s])",
            body,
        )
        match = standalone or label_context or actor_context or role_as_name
        if match is None:
            continue
        start = match.start(2) if match.lastindex and match.lastindex >= 2 else match.start(1)
        subject = f"placeholder:{placeholder}:bare_role"
        repair_hint = (
            f"删除正文中所有作为角色标签的「{placeholder}」。"
            "改用具体姓名，或改成可追踪且非占位的稳定代号，例如「旧书摊主」「地下管理员」；"
            f"修复后正文和 summary 都不应再用「{placeholder}」称呼关键行动者。"
        )
        return CanonQualitySignal(
            signal_id=make_signal_id(project_id, chapter_number, "bare_role_placeholder_leakage", subject),
            project_id=project_id,
            chapter_number=chapter_number,
            signal_type="bare_role_placeholder_leakage",
            severity="error",
            target_scope="body",
            subject_key=subject,
            description=(
                f"章节正文把泛称「{placeholder}」作为独立角色标签使用；关键行动者应使用姓名、阵营职能或可追踪代号。"
            ),
            evidence_refs=[f"body:{start}-{start + len(placeholder)}"],
            span_start=start,
            span_end=start + len(placeholder),
            payload={"draft_id": draft_id, "placeholder": placeholder, "repair_hint": repair_hint},
        )
    return None


def _analyze_protagonist_placeholder(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    body: str,
    summary: str,
    expected_character_names: set[str],
) -> CanonQualitySignal | None:
    expected = sorted(
        {
            str(name or "").strip()
            for name in expected_character_names
            if 2 <= len(str(name or "").strip()) <= 4
        },
        key=len,
        reverse=True,
    )
    if not expected:
        return None
    if any(name in body for name in expected):
        return None

    for placeholder in PROTAGONIST_PLACEHOLDER_ROLES:
        count = _term_count(body, placeholder)
        if count < 5 and placeholder not in summary:
            continue
        first = body.find(placeholder)
        if first < 0:
            first = summary.find(placeholder)
        expected_text = "、".join(expected[:3])
        subject = f"placeholder:{placeholder}:protagonist"
        return CanonQualitySignal(
            signal_id=make_signal_id(project_id, chapter_number, "protagonist_placeholder_leakage", subject),
            project_id=project_id,
            chapter_number=chapter_number,
            signal_type="protagonist_placeholder_leakage",
            severity="error",
            target_scope="body",
            subject_key=subject,
            description=(
                f"章节疑似用泛称「{placeholder}」替代主角姓名；预期主角/核心角色应出现「{expected_text}」。"
            ),
            evidence_refs=[f"body:{max(first, 0)}-{max(first, 0) + len(placeholder)}"],
            span_start=max(first, 0),
            span_end=max(first, 0) + len(placeholder),
            payload={
                "draft_id": draft_id,
                "placeholder": placeholder,
                "expected_character_names": expected,
                "placeholder_count": count,
            },
        )
    return None


def extract_expected_protagonist_names(*values: str) -> set[str]:
    names: set[str] = set()
    text = "\n".join(str(value or "") for value in values if str(value or "").strip())
    for match in re.finditer(r"(?:主角|主人公|主视角)\s*(?:[：:是为]\s*|\s+)([\u4e00-\u9fff]{2,4})", text):
        name = match.group(1).strip()
        if name and name not in PROTAGONIST_PLACEHOLDER_ROLES:
            names.add(name)
    for match in re.finditer(r'"name"\s*:\s*"([\u4e00-\u9fff]{2,4})"', text):
        name = match.group(1).strip()
        if name and name not in PROTAGONIST_PLACEHOLDER_ROLES:
            names.add(name)
    return names


def _term_count(text: str, term: str) -> int:
    if not term:
        return 0
    return sum(1 for _ in re.finditer(re.escape(term), text))
