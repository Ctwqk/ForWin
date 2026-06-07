"""Prompt builders for the ForWin chapter generation system.

All user-facing text in the prompts is written in Chinese so that the LLM
produces fluent Chinese web-novel prose and metadata without code-switching.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from forwin.canon_names import canon_name_anchor_lines, extract_canon_name_anchors
from forwin.canon_quality.rule_profile import (
    countdown_profiles_from_quality_context,
    display_countdown_label,
)
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.scene import SceneOutput, ScenePlan


@dataclass(frozen=True)
class ConstraintSection:
    key: str
    priority: int
    must_inject: bool
    text: str
    max_chars: int = 0


def _canon_quality_context_section(context: ChapterContextPack) -> str | None:
    quality = getattr(context, "canon_quality_context", {}) or {}
    if not isinstance(quality, dict):
        return None
    countdown_profiles = countdown_profiles_from_quality_context(quality)
    countdown_constraints = [
        item for item in quality.get("countdown_constraints", []) or [] if isinstance(item, dict)
    ]
    invariant_constraints = [
        item for item in quality.get("invariant_constraints", []) or [] if isinstance(item, dict)
    ]
    character_state_constraints = [
        item
        for item in quality.get("character_state_constraints", []) or []
        if isinstance(item, dict)
    ]
    open_signals = [item for item in quality.get("open_signals", []) or [] if isinstance(item, dict)]
    active_obligations = [
        item
        for item in quality.get("active_narrative_obligations", []) or []
        if isinstance(item, dict)
    ]
    structural_patch_debt = [
        item
        for item in quality.get("active_structural_patch_debt", []) or []
        if isinstance(item, dict)
    ]
    future_plan_audit_summary = quality.get("future_plan_audit_summary", {})
    if not isinstance(future_plan_audit_summary, dict):
        future_plan_audit_summary = {}
    is_final_chapter = bool(
        quality.get("is_final_chapter")
        or (
            int(getattr(context, "project_target_total_chapters", 0) or 0)
            and int(getattr(context, "chapter_number", 0) or 0)
            >= int(getattr(context, "project_target_total_chapters", 0) or 0)
        )
    )
    if not any((
        countdown_constraints,
        invariant_constraints,
        character_state_constraints,
        open_signals,
        active_obligations,
        structural_patch_debt,
        future_plan_audit_summary,
        is_final_chapter,
    )):
        return None
    suppressed = _suppressed_prompt_constraint_keys(quality)
    countdown_constraints = _visible_countdown_constraints(countdown_constraints, suppressed=suppressed)
    invariant_constraints = _visible_invariant_constraints(invariant_constraints, suppressed=suppressed)
    open_signals = _visible_open_signal_constraints(open_signals, suppressed=suppressed)
    active_obligations = _visible_obligation_constraints(active_obligations, suppressed=suppressed)
    _record_prompt_constraint_counts(
        quality,
        original_countdowns=quality.get("countdown_constraints", []) or [],
        original_invariants=quality.get("invariant_constraints", []) or [],
        original_open_signals=quality.get("open_signals", []) or [],
        original_obligations=quality.get("active_narrative_obligations", []) or [],
        visible_countdowns=countdown_constraints,
        visible_invariants=invariant_constraints,
        visible_open_signals=open_signals,
        visible_obligations=active_obligations,
    )
    sections = [
        *_final_chapter_constraint_section(is_final_chapter),
        *_invariant_constraint_sections(invariant_constraints),
        *_countdown_constraint_sections(countdown_constraints, profiles=countdown_profiles),
        *_character_state_constraint_sections(character_state_constraints),
        *_open_signal_constraint_sections(open_signals, suppressed=set()),
        *_future_plan_audit_sections(future_plan_audit_summary),
        *_active_obligation_constraint_sections(active_obligations, suppressed=set()),
        *_structural_patch_debt_sections(structural_patch_debt),
    ]
    return _render_constraint_sections(
        sections,
        max_chars=int(quality.get("canon_quality_prompt_budget_chars") or 4200),
    )


def _render_constraint_sections(sections: list[ConstraintSection], *, max_chars: int) -> str | None:
    ordered = sorted(
        [section for section in sections if section.text.strip()],
        key=lambda item: (item.priority, item.key),
    )
    if not ordered:
        return None
    lines = ["【Canon 质量连续性约束】"]
    current = len(lines[0])
    for section in ordered:
        text = section.text.strip()
        section_limit = int(section.max_chars or 0)
        if section_limit and len(text) > section_limit:
            text = text[:section_limit].rstrip() + "..."
        would_be = current + len(text) + 1
        if max_chars > 0 and would_be > max_chars and not section.must_inject:
            continue
        lines.append(text)
        current += len(text) + 1
    return "\n".join(lines) if len(lines) > 1 else None


def _final_chapter_constraint_section(is_final_chapter: bool) -> list[ConstraintSection]:
    if not is_final_chapter:
        return []
    return [
        ConstraintSection(
            key="final_chapter",
            priority=10,
            must_inject=True,
            text="\n".join(
                [
                    "  · 本章是全书终章或当前目标终章，必须在正文内完成主线危机的关闭、公开、阻止或明确代价结算。",
                    "  · 终章不得以追兵逼近、被困、关键道具损坏、准备公开、正要关闭、等待下一步等主线未完成动作作结。",
                    "  · 如果写到关闭方法、关键道具、坐标或入口，必须在本章完成使用、关闭或公开；不要只把它们作为下一步任务。",
                    "  · 如需留余味，只能留在主线危机已解决之后，作为轻量后日谈或续作暗线。",
                ]
            ),
        )
    ]


def _countdown_constraint_sections(
    countdown_constraints: list[dict],
    *,
    profiles: dict,
) -> list[ConstraintSection]:
    if not countdown_constraints:
        return []
    lines = [
        "  · 已进入 canon 的倒计时 ledger 上限：",
        "    · 硬性优先级：下面的 ledger 上限优先于前情摘要、章节计划和旧设定；旧摘要里的旧时间只代表当时状态，不能覆盖最新 ledger。",
    ]
    for item in countdown_constraints[:6]:
        key = str(item.get("countdown_key") or "").strip()
        label = _display_countdown_label(key=key, label=str(item.get("label") or ""), profiles=profiles)
        latest = int(item.get("latest_remaining_minutes") or 0)
        chapter = int(item.get("latest_chapter") or 0)
        raw = str(item.get("raw_mention") or "").strip()
        raw_suffix = f"（原文：{raw}）" if raw else ""
        lines.append(
            f"    · {label}：第{chapter}章已剩余 {latest} 分钟{raw_suffix}；"
            f"本章继续同一倒计时时必须小于等于 {latest} 分钟，除非正文明确 reset 或声明为另一个分支倒计时。"
        )
        if latest <= 0:
            lines.append(
                f"      · {label}已经归零、关闭或解决；本章不得再写成正数剩余时间。"
                "若确实开启了新的局部窗口，必须明确写出新的开启事件和不同窗口名称。"
            )
            continue
        profile = profiles.get(key)
        forbidden_phrases = [
            str(value).strip()
            for value in (getattr(profile, "forbidden_stale_phrases", []) if profile else [])
            if str(value).strip()
        ]
        forbidden_text = (
            "，不得写回 " + "、".join(forbidden_phrases[:8]) + " 等旧尺度"
            if forbidden_phrases
            else "，不得写成任何大于最新 ledger 的旧尺度"
        )
        lines.append(
            f"      · {label}硬性规则：只能写小于等于 {latest} 分钟{forbidden_text}；"
            "若另写局部窗口，必须明确命名为不同倒计时，不能改变本 ledger。"
        )
        if latest <= 180:
            lines.append(
                f"      · 计划覆盖：当前{label}已进入 {latest} 分钟级危机。"
                "本章计划、标题或旧摘要中任何大于最新 ledger 的旧说法都视为过期计划，"
                f"必须改写为小于等于 {latest} 分钟的连续倒计时。"
            )
        lines.append(
            f"      · 章内单调规则：本章如果多次写{label}剩余时间，必须按出现顺序严格不增加；"
            "局部时长必须明确命名为局部时长，不得写成本 ledger 的剩余时间。"
        )
    return [
        ConstraintSection(
            key="countdown",
            priority=20,
            must_inject=True,
            text="\n".join(lines),
        )
    ]


def _character_state_constraint_sections(items: list[dict]) -> list[ConstraintSection]:
    if not items:
        return []
    lines = ["  · 已进入 canon 的角色状态约束："]
    for item in items[:6]:
        character_name = str(item.get("character_name") or "").strip()
        latest_state = str(item.get("latest_state") or "").strip()
        chapter = int(item.get("latest_chapter") or 0)
        if not character_name or not latest_state:
            continue
        if latest_state in {"free", "released", "rescued", "escaped"}:
            lines.append(
                f"    · {character_name}：第{chapter}章已脱困/自由；本章不得把TA写回被捕、被关押、"
                "被羁押、被固定、仍在羁押室或等待救援，除非正文先写清楚新的再次被捕桥接。"
                "可以写TA仍受追踪器、系统权限、伤势或路线限制。"
            )
        else:
            lines.append(
                f"    · {character_name}：第{chapter}章最新状态为 {latest_state}；"
                "本章必须承接该状态，状态改变需要明确桥接事件。"
            )
    return [ConstraintSection(key="character_state", priority=30, must_inject=True, text="\n".join(lines))]


def _invariant_constraint_sections(items: list[dict]) -> list[ConstraintSection]:
    if not items:
        return []
    lines = [
        "  · 强状态 invariant ledger：",
        "    · 这些状态优先于前情摘要、章节计划和旧设定；改写它们必须在正文内写出明确桥接事件。",
    ]
    for item in items[:8]:
        invariant_key = str(item.get("invariant_key") or "").strip()
        kind = str(item.get("kind") or "custom").strip()
        label = str(item.get("label") or item.get("subject_key") or invariant_key).strip()
        latest_chapter = int(item.get("latest_chapter") or item.get("last_updated_chapter") or 0)
        current_value = item.get("current_value")
        value_unit = str(item.get("value_unit") or "").strip()
        if not invariant_key:
            continue
        if kind == "monotonic_numeric" and value_unit == "minutes":
            latest = _optional_int(current_value)
            if latest is None:
                lines.append(
                    f"    · {label}：当前是分钟级单调强状态；本章继续同一状态时不得增大，除非正文明确 reset、reopen 或分支窗口。"
                )
                continue
            lines.append(
                f"    · {label}：第{latest_chapter}章 ledger 当前值为 {latest} 分钟；"
                f"本章继续同一状态时必须小于等于 {latest} 分钟，除非正文明确 reset、reopen 或分支窗口。"
            )
            continue
        if kind == "deadline":
            lines.append(
                f"    · {label}：当前截止状态为 {_compact_value(current_value)}；"
                "本章不得把截止条件静默延后、取消或改名；如要改变，必须写出明确桥接事件、代价或授权来源。"
            )
            continue
        if kind == "state_transition":
            lines.append(
                f"    · {label}：当前状态为 {_compact_value(current_value)}；"
                "本章必须承接该状态，状态改变需要明确桥接事件。"
            )
            continue
        if kind == "active_rule":
            lines.append(
                f"    · {label}：当前 active rule 仍生效；本章必须遵守规则边界，撤销或豁免需要正文证据。"
            )
            continue
        lines.append(
            f"    · {label}：当前强状态为 {_compact_value(current_value)}；本章必须承接，改写需要明确桥接事件。"
        )
    return [ConstraintSection(key="invariants", priority=18, must_inject=True, text="\n".join(lines))]


def _open_signal_constraint_sections(items: list[dict], *, suppressed: set[str]) -> list[ConstraintSection]:
    visible = [
        item for item in items
        if _constraint_identity("signal", str(item.get("signal_id") or item.get("subject_key") or "")) not in suppressed
    ]
    if not visible:
        return []
    lines = ["  · 前文 residual quality signals，后续写作必须解释、修复或避免扩大："]
    for item in visible[:5]:
        severity = str(item.get("severity") or "").strip()
        chapter = int(item.get("chapter_number") or 0)
        description = str(item.get("description") or "").strip()
        if description:
            lines.append(f"    · 第{chapter}章 {severity}：{description[:180]}")
    return [ConstraintSection(key="open_signals", priority=70, must_inject=False, text="\n".join(lines), max_chars=900)]


def _future_plan_audit_sections(summary: dict) -> list[ConstraintSection]:
    if not summary:
        return []
    status = str(summary.get("status") or "").strip()
    patch_ids = [
        str(item).strip()
        for item in summary.get("applied_plan_patch_ids", []) or []
        if str(item).strip()
    ]
    lines = [f"  · Future plan audit：status={status or 'unknown'}"]
    if patch_ids:
        lines.append(f"    · 已应用计划补丁：{', '.join(patch_ids[:5])}")
    for item in [raw for raw in summary.get("issues", []) or [] if isinstance(raw, dict)][:5]:
        issue_type = str(item.get("issue_type") or "").strip()
        chapter = int(item.get("target_chapter") or 0)
        description = str(item.get("description") or "").strip()
        lines.append(f"    · 第{chapter}章 {issue_type}：{description[:180]}")
    return [ConstraintSection(key="future_plan_audit", priority=40, must_inject=True, text="\n".join(lines))]


def _active_obligation_constraint_sections(items: list[dict], *, suppressed: set[str]) -> list[ConstraintSection]:
    visible = [
        item for item in items
        if _constraint_identity("obligation", str(item.get("id") or "")) not in suppressed
    ]
    if not visible:
        return []
    lines = ["  · 当前生效的叙事义务，必须在 deadline 前偿还；must_resolve_now=true 的条目本章必须给出证据："]
    for item in visible[:6]:
        obligation_id = str(item.get("id") or "").strip()
        obligation_type = str(item.get("type") or "").strip()
        priority = str(item.get("priority") or "").strip()
        summary = str(item.get("summary") or "").strip()
        deadline = int(item.get("deadline_chapter") or 0)
        payoff_test = str(item.get("payoff_test") or "").strip()
        must_resolve_now = bool(item.get("must_resolve_now"))
        lines.append(
            f"    · {priority} {obligation_type} {obligation_id}：{summary[:140]}；"
            f"deadline=第{deadline}章；must_resolve_now={str(must_resolve_now).lower()}；"
            f"payoff_test={payoff_test[:160]}"
        )
    return [ConstraintSection(key="active_obligations", priority=60, must_inject=False, text="\n".join(lines), max_chars=1100)]


def _structural_patch_debt_sections(items: list[dict]) -> list[ConstraintSection]:
    if not items:
        return []
    lines = ["  · 结构性计划补丁债务，后续章节必须偿还对应 payoff_tests："]
    for item in items[:6]:
        patch_id = str(item.get("patch_id") or "").strip()
        scope = str(item.get("scope") or "").strip()
        payoff_tests = [
            str(test).strip()
            for test in item.get("payoff_tests", []) or []
            if str(test).strip()
        ]
        injections = [
            raw for raw in item.get("writer_context_injections", []) or [] if isinstance(raw, dict)
        ]
        instruction = ""
        if injections:
            instruction = str(injections[0].get("instruction") or "").strip()
        payoff = "；".join(payoff_tests[:3])
        lines.append(
            f"    · {scope} patch {patch_id}：{instruction[:140]}；payoff_tests={payoff[:180]}"
        )
    return [
        ConstraintSection(
            key="structural_patch_debt",
            priority=55,
            must_inject=True,
            text="\n".join(lines),
            max_chars=1200,
        )
    ]


def _suppressed_prompt_constraint_keys(quality: dict) -> set[str]:
    raw_values = quality.get("suppressed_prompt_constraint_keys", []) or []
    return {str(item).strip() for item in raw_values if str(item).strip()}


def _visible_countdown_constraints(items: list[dict], *, suppressed: set[str]) -> list[dict]:
    return [
        item
        for item in items
        if _constraint_identity("countdown", str(item.get("countdown_key") or item.get("key") or "")) not in suppressed
        and _constraint_identity(
            "invariant",
            f"countdown:{str(item.get('countdown_key') or item.get('key') or '').strip()}",
        )
        not in suppressed
    ]


def _visible_invariant_constraints(items: list[dict], *, suppressed: set[str]) -> list[dict]:
    return [
        item
        for item in items
        if _constraint_identity("invariant", str(item.get("invariant_key") or "")) not in suppressed
    ]


def _visible_open_signal_constraints(items: list[dict], *, suppressed: set[str]) -> list[dict]:
    return [
        item
        for item in items
        if _constraint_identity("signal", str(item.get("signal_id") or item.get("subject_key") or "")) not in suppressed
    ]


def _visible_obligation_constraints(items: list[dict], *, suppressed: set[str]) -> list[dict]:
    return [
        item
        for item in items
        if _constraint_identity("obligation", str(item.get("id") or "")) not in suppressed
    ]


def _record_prompt_constraint_counts(
    quality: dict,
    *,
    original_countdowns: list,
    original_invariants: list,
    original_open_signals: list,
    original_obligations: list,
    visible_countdowns: list,
    visible_invariants: list,
    visible_open_signals: list,
    visible_obligations: list,
) -> None:
    original_count = (
        len([item for item in original_countdowns if isinstance(item, dict)])
        + len([item for item in original_invariants if isinstance(item, dict)])
        + len([item for item in original_open_signals if isinstance(item, dict)])
        + len([item for item in original_obligations if isinstance(item, dict)])
    )
    remaining_count = len(visible_countdowns) + len(visible_invariants) + len(visible_open_signals) + len(visible_obligations)
    quality["form_prompt_constraints_suppressed"] = max(original_count - remaining_count, 0)
    quality["form_prompt_constraints_remaining"] = remaining_count


def _constraint_identity(kind: str, value: str) -> str:
    return f"{kind}:{str(value or '').strip()}"


def _display_countdown_label(*, key: str, label: str, profiles: dict | None = None) -> str:
    return display_countdown_label(key=key, label=label, profiles=profiles)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _compact_value(value: object) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    text = str(value or "").strip()
    return text or "未知"


__all__ = [
    'ConstraintSection',
    '_canon_quality_context_section',
    '_render_constraint_sections',
    '_final_chapter_constraint_section',
    '_countdown_constraint_sections',
    '_invariant_constraint_sections',
    '_character_state_constraint_sections',
    '_open_signal_constraint_sections',
    '_future_plan_audit_sections',
    '_active_obligation_constraint_sections',
    '_structural_patch_debt_sections',
    '_suppressed_prompt_constraint_keys',
    '_visible_countdown_constraints',
    '_visible_invariant_constraints',
    '_visible_open_signal_constraints',
    '_visible_obligation_constraints',
    '_constraint_identity',
    '_display_countdown_label',
]
