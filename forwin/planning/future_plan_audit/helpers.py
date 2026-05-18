from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.base import new_id
from forwin.models.narrative_obligation import FuturePlanAuditRunRow
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.planning.obligation_pre_audit import select_urgent_obligation_targets
from forwin.planning.plan_patch_validator import PlanPatchValidator
from forwin.planning.signal_pre_audit import select_stale_signal_targets
from forwin.protocol.experience import BandDelightSchedule

from .models import FuturePlanAuditIssue


_CUSTODY_FREE_STATES = {"free", "released", "rescued", "escaped"}
_CUSTODY_PLAN_STALE_MARKERS = (
    "被捕状态",
    "仍被捕",
    "仍被关押",
    "仍被羁押",
    "继续被关押",
    "继续被羁押",
    "被关押",
    "被关在",
    "被关进",
    "被扣押",
    "被捕",
    "被固定",
    "被束缚",
    "被锁在",
    "被磁扣锁",
    "被磁力铐",
    "临时羁押室",
    "羁押室",
    "救援窗口",
    "营救窗口",
    "救出",
    "营救",
)
_CUSTODY_RECAPTURE_MARKERS = (
    "再次被捕",
    "再度被捕",
    "重新被捕",
    "又被捕",
    "又被带走",
    "重新关押",
    "被重新关押",
    "再度关押",
    "重新关进",
    "再次关进",
    "又被关",
    "重新控制",
    "被押回",
    "被抓回",
    "被拖回",
)
_DURATION_RE = re.compile(
    r"(不到|不超过|约|大约|剩余|还有|还剩|第)?\s*([0-9]+|[零一二两三四五六七八九十百]+)\s*(分钟|分|小时|钟头|天|日)"
)
_COUNTDOWN_INSTRUCTION_RE = re.compile(
    r"(?:[A-Za-z_]+|[\u4e00-\u9fff]+)必须延续最新 canon ledger：剩余时间不得超过(?:不超过)?\s*[0-9零一二两三四五六七八九十百]+\s*分钟。?"
)


def _countdown_constraints(canon_quality_context: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(canon_quality_context, dict):
        return []
    return [
        item
        for item in canon_quality_context.get("countdown_constraints", []) or []
        if isinstance(item, dict)
    ]

def _character_state_constraints(canon_quality_context: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(canon_quality_context, dict):
        return []
    return [
        item
        for item in canon_quality_context.get("character_state_constraints", []) or []
        if isinstance(item, dict)
    ]

def _open_signal_constraints(canon_quality_context: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(canon_quality_context, dict):
        return []
    return [
        item
        for item in canon_quality_context.get("open_signals", []) or []
        if isinstance(item, dict)
    ]

def _chapter_plan_contract(plan: ChapterPlan) -> dict[str, Any]:
    return {
        "chapter_number": int(plan.chapter_number or 0),
        "title": str(plan.title or ""),
        "one_line": str(plan.one_line or ""),
        "goals": _loads(plan.goals_json, []),
        "task_contract": _loads(plan.task_contract_json, []),
        "experience_plan": _loads(plan.experience_plan_json, {}),
    }

def _plan_has_countdown_instruction_pollution(plan: ChapterPlan) -> bool:
    if "必须延续最新 canon ledger" in str(plan.one_line or ""):
        return True
    if any("必须延续最新 canon ledger" in str(item) for item in _loads(plan.goals_json, [])):
        return True
    if any("不超过不超过" in str(item) for item in _loads(plan.goals_json, [])):
        return True
    experience = _loads(plan.experience_plan_json, {})
    if not isinstance(experience, dict):
        return False
    for key, raw_items in experience.items():
        items = raw_items if isinstance(raw_items, list) else [raw_items]
        for item in items:
            text = str(item)
            if "不超过不超过" in text:
                return True
            if key != "rule_anchors" and "必须延续最新 canon ledger" in text:
                return True
    return False

def _plan_text(plan: ChapterPlan) -> str:
    payload = _chapter_plan_contract(plan)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)

def _is_custody_state_patch(patch: NarrativePlanPatch) -> bool:
    metadata = patch.metadata if isinstance(patch.metadata, dict) else {}
    if metadata.get("conflict_type") == "custody_state":
        return True
    return str(patch.new_contract.get("transition_type") or "") == "custody_state"

def _minimum_scope_for_obligation(obligation: NarrativeObligation) -> str:
    metadata = obligation.metadata if isinstance(obligation.metadata, dict) else {}
    explicit = str(metadata.get("minimum_scope") or "").strip()
    if explicit:
        return explicit
    if obligation.obligation_type in {
        "reader_promise_payoff",
        "reveal_escalation_needed",
        "style_repetition_pressure",
        "repeated_scene_pattern",
    }:
        return "band"
    return "chapter"

def _band_row_for_obligation(
    *,
    obligation: NarrativeObligation,
    band_rows: list[BandExperiencePlan],
    current_chapter: int,
) -> BandExperiencePlan | None:
    deadline = int(obligation.deadline_chapter or 0)
    for row in band_rows:
        if int(row.chapter_start or 0) <= deadline <= int(row.chapter_end or 0):
            return row
    for row in band_rows:
        if int(row.chapter_end or 0) > int(current_chapter or 0):
            return row
    return None

def _band_contract_covers_obligation(row: BandExperiencePlan, obligation: NarrativeObligation) -> bool:
    obligation_id = str(obligation.id or "").strip()
    if not obligation_id:
        return False
    try:
        payload = json.loads(row.schedule_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        schedule = BandDelightSchedule.model_validate(payload)
    except Exception:
        return False
    contract = schedule.band_obligation_contract
    if obligation_id not in contract.open_obligations:
        return False
    if obligation_id not in contract.payoff_tests:
        return False
    return bool(str(contract.payoff_tests.get(obligation_id) or "").strip())

def _plan_mentions_stale_custody(text: str, *, character_name: str) -> bool:
    if not character_name or character_name not in text:
        return False
    clauses = re.split(r"[，,。；;！!？?\n]+", text)
    for clause in clauses:
        if not clause.strip():
            continue
        if not any(marker in clause for marker in _CUSTODY_PLAN_STALE_MARKERS):
            continue
        if character_name not in clause and "救援窗口" not in clause and "营救窗口" not in clause:
            continue
        if _clause_negates_custody_conflict(clause):
            continue
        return True
    return False

def _plan_declares_recapture_bridge(text: str, *, character_name: str) -> bool:
    if not character_name or character_name not in text:
        return False
    return any(marker in text for marker in _CUSTODY_RECAPTURE_MARKERS)

def _clause_negates_custody_conflict(clause: str) -> bool:
    return any(
        marker in clause
        for marker in (
            "不得把",
            "不要把",
            "不能把",
            "禁止把",
            "避免把",
            "不得将",
            "不要将",
            "不能将",
            "禁止将",
            "不得写",
            "不要写",
            "不能写",
            "禁止写",
            "不得让",
            "不要让",
            "不能让",
            "禁止让",
        )
    )

def _hard_custody_instruction(*, character_name: str, latest_chapter: int) -> str:
    prefix = f"{character_name}已在第{latest_chapter}章脱困" if latest_chapter else f"{character_name}已脱困"
    return (
        f"{prefix}；本章必须承接已脱困但仍受追踪器或系统权限限制的状态，"
        f"不得把{character_name}写回被捕/羁押/固定状态。"
        "如果剧情需要再次关押，必须先写出明确的再次被捕桥接事件。"
    )

def _rewrite_stale_custody_text(text: str, *, character_name: str) -> str:
    if not text or not character_name:
        return text
    replacements = {
        f"在不破坏{character_name}被捕状态的前提下": f"在承接{character_name}已脱困但仍受追踪器或系统权限限制的前提下",
        "在不破坏被捕状态的前提下": "在承接已脱困但仍受追踪器或系统权限限制的前提下",
        f"{character_name}仍被羁押": f"{character_name}已脱困但仍受追踪器或系统权限限制",
        f"{character_name}仍被关押": f"{character_name}已脱困但仍受追踪器或系统权限限制",
        f"{character_name}被羁押": f"{character_name}受追踪器或系统权限限制",
        f"{character_name}被关押": f"{character_name}受追踪器或系统权限限制",
        f"救出{character_name}": f"保护已脱困的{character_name}并解除追踪器",
        f"营救{character_name}": f"保护已脱困的{character_name}并解除追踪器",
        "救援窗口": "追踪器解除窗口",
        "营救窗口": "追踪器解除窗口",
        "被捕状态": "已脱困但仍受追踪器或系统权限限制的状态",
        "仍被羁押": "已脱困但仍受追踪器或系统权限限制",
        "仍被关押": "已脱困但仍受追踪器或系统权限限制",
        "被羁押": "受追踪器或系统权限限制",
        "被关押": "受追踪器或系统权限限制",
        "临时羁押室": "临时监听点",
        "羁押室": "监听点",
        "被固定": "受权限限制",
        "被束缚": "受权限限制",
        "被锁在": "受权限限制于",
        "被磁力铐": "受追踪器",
        "被磁扣锁": "受追踪器",
    }
    output = text
    for old, new in replacements.items():
        output = output.replace(old, new)
    return output

def _rewrite_custody_json_strings(value: Any, *, character_name: str) -> Any:
    if isinstance(value, str):
        return _rewrite_stale_custody_text(value, character_name=character_name)
    if isinstance(value, list):
        return [_rewrite_custody_json_strings(item, character_name=character_name) for item in value]
    if isinstance(value, dict):
        return {
            item_key: _rewrite_custody_json_strings(item, character_name=character_name)
            for item_key, item in value.items()
        }
    return value

def _duration_mentions_for_countdown(text: str, *, key: str, label: str) -> list[dict[str, Any]]:
    clauses = re.split(r"[，,。；;！!？?\n]+", text)
    mentions: list[dict[str, Any]] = []
    countdown_clause_indexes = {
        index
        for index, clause in enumerate(clauses)
        if _clause_mentions_countdown(clause, key=key, label=label)
    }
    for index, clause in enumerate(clauses):
        mentions_countdown = index in countdown_clause_indexes
        continues_previous_countdown = (
            index - 1 in countdown_clause_indexes
            and _clause_continues_countdown_duration(clause)
            and not _clause_mentions_other_countdown(clause, key=key, label=label)
        )
        if not mentions_countdown and not continues_previous_countdown:
            continue
        if _clause_declares_reset_or_branch(clause):
            continue
        for mention in _duration_mentions(clause):
            mention["clause"] = clause
            mention["context"] = "。".join(
                str(item)
                for item in (
                    clauses[index - 1] if index > 0 else "",
                    clause,
                    clauses[index + 1] if index + 1 < len(clauses) else "",
                )
                if str(item).strip()
            )
            mentions.append(mention)
    return mentions

def _is_false_prior_countdown_clause(clause: str) -> bool:
    text = str(clause or "")
    if _clause_declares_reset_or_branch(text):
        return False
    return any(
        marker in text
        for marker in (
            "accepted canon",
            "最新 canon",
            "最新ledger",
            "最新 ledger",
            "canon ledger",
            "已接受 canon",
            "已 accepted",
            "上一章 accepted",
            "承接上一章",
            "连续性护栏",
            "必须紧接此状态",
        )
    )

def _hard_countdown_instruction(*, label: str, key: str, latest: int) -> str:
    if latest <= 0:
        return (
            f"{label}已关闭；本章不得把同一倒计时写成仍有剩余时间，"
            "除非明确标记为 reset 或 branch clock。"
        )
    base = (
        f"{label}必须延续最新 canon ledger：剩余时间不得超过 {latest} 分钟。"
        "旧计划/旧摘要时间不得写成前文事实；"
        "不得写“系统日志原本还有三天/七天/几小时”、"
        "“主角以为还有几天”或任何大于最新 ledger 的旧尺度，"
        f"除非明确标记为公开伪数据、误导信息、reset 或 branch clock。"
    )
    if key == "memory_reset" or "记忆重置" in label or "重置周期" in label:
        return (
            base
            + f" 本章所有记忆重置/校准/熔铸窗口只能继续小于等于 {latest} 分钟，"
            "不要写回三天/七天/三小时/两小时等旧尺度。"
        )
    return base

def _clause_mentions_countdown(clause: str, *, key: str, label: str) -> bool:
    candidates = _countdown_markers_for(key=key, label=label)
    return any(candidate and candidate in clause for candidate in candidates)

def _clause_mentions_other_countdown(clause: str, *, key: str, label: str) -> bool:
    current = set(_countdown_markers_for(key=key, label=label))
    for other_key, other_label in (
        ("memory_reset", "记忆重置周期"),
        ("archive_cleanup", "档案清理窗口"),
        ("terminal_audit_window", "终端审计窗口"),
        ("core_access_window", "核心层授权窗口"),
        ("public_countdown", "公开数据倒计时"),
        ("main", "主倒计时"),
    ):
        if other_key == key:
            continue
        for candidate in _countdown_markers_for(key=other_key, label=other_label):
            if candidate and candidate not in current and candidate in clause:
                return True
    return False

def _countdown_markers_for(*, key: str, label: str) -> list[str]:
    candidates = [key, label]
    if key == "memory_reset" or "记忆" in label or "重置" in label:
        candidates.extend(["记忆重置", "重置周期", "记忆熔铸", "熔铸倒计时", "熔铸窗口", "memory_reset"])
    if key == "archive_cleanup" or "档案清理" in label:
        candidates.extend(["档案清理", "清理窗口", "archive_cleanup"])
    if key == "terminal_audit_window" or "终端审计" in label:
        candidates.extend(["终端审计", "终端审计窗口", "terminal_audit_window"])
    if key == "core_access_window" or "核心层" in label or "授权" in label:
        candidates.extend(["核心层入口", "核心层授权窗口", "核心层的授权窗口", "入口关闭", "core_access_window"])
    if key == "public_countdown" or "公开" in label:
        candidates.extend(["公开数据", "公开窗口", "对外数据", "public_countdown"])
    if key == "main":
        candidates.extend(["主倒计时", "倒计时"])
    return candidates

def _clause_continues_countdown_duration(clause: str) -> bool:
    if not _duration_mentions(clause):
        return False
    return any(marker in clause for marker in ("只剩", "还剩", "剩余", "还有", "距离", "窗口", "提前至", "缩短到"))

def _clause_declares_reset_or_branch(clause: str) -> bool:
    return any(marker in clause for marker in ("分支倒计时", "branch", "另一个倒计时", "新的倒计时", "重新开始", "重置为"))

def _duration_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    for match in _DURATION_RE.finditer(text):
        amount = _parse_amount(match.group(2))
        if amount <= 0:
            continue
        unit = match.group(3)
        multiplier = 1
        if unit in {"小时", "钟头"}:
            multiplier = 60
        elif unit in {"天", "日"}:
            multiplier = 24 * 60
        mentions.append(
            {
                "raw": match.group(0).strip(),
                "minutes": amount * multiplier,
                "span_start": match.start(),
                "span_end": match.end(),
            }
        )
    return mentions

def _parse_amount(raw: str) -> int:
    value = str(raw or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    return _parse_chinese_number(value)

def _parse_chinese_number(value: str) -> int:
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "百" in value:
        left, _, right = value.partition("百")
        return (digits.get(left, 1) or 1) * 100 + _parse_chinese_number(right)
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for char in value:
        total = total * 10 + digits.get(char, 0)
    return total

def _rewrite_stale_countdown_text(
    text: str,
    *,
    key: str,
    label: str,
    latest: int,
    rewrite_false_prior: bool = False,
) -> str:
    if not text or latest < 0:
        return text
    parts = re.split(r"([，,。；;！!？?\n]+)", text)
    clause_indexes = [index for index in range(0, len(parts), 2)]
    countdown_clause_indexes = {
        index
        for index in clause_indexes
        if _clause_mentions_countdown(parts[index], key=key, label=label)
    }
    output: list[str] = []
    previous_clause_index: int | None = None
    for index, part in enumerate(parts):
        if index not in clause_indexes:
            output.append(part)
            continue
        mentions_countdown = index in countdown_clause_indexes
        continues_previous_countdown = (
            previous_clause_index in countdown_clause_indexes
            and _clause_continues_countdown_duration(part)
            and not _clause_mentions_other_countdown(part, key=key, label=label)
        )
        if (mentions_countdown or continues_previous_countdown) and not _clause_declares_reset_or_branch(part):
            surrounding = "。".join(
                str(parts[item])
                for item in (index - 2, index, index + 2)
                if item in clause_indexes and 0 <= item < len(parts) and str(parts[item]).strip()
            )
            part = _replace_stale_duration_mentions(
                part,
                latest=latest,
                rewrite_false_prior=(
                    rewrite_false_prior and _is_false_prior_countdown_clause(surrounding)
                ),
            )
        output.append(part)
        previous_clause_index = index
    return "".join(output)

def _replace_stale_duration_mentions(text: str, *, latest: int, rewrite_false_prior: bool = False) -> str:
    def replace(match: re.Match[str]) -> str:
        amount = _parse_amount(match.group(2))
        unit = match.group(3)
        multiplier = 1
        if unit in {"小时", "钟头"}:
            multiplier = 60
        elif unit in {"天", "日"}:
            multiplier = 24 * 60
        minutes = amount * multiplier
        if latest <= 0 and minutes > 0:
            return "已关闭"
        if minutes <= latest and not (rewrite_false_prior and minutes < latest):
            return match.group(0)
        return f"不超过{latest}分钟"

    return _DURATION_RE.sub(replace, text)

def _rewrite_json_strings(
    value: Any,
    *,
    key: str,
    label: str,
    latest: int,
    rewrite_false_prior: bool = False,
) -> Any:
    if isinstance(value, str):
        return _rewrite_stale_countdown_text(
            value,
            key=key,
            label=label,
            latest=latest,
            rewrite_false_prior=rewrite_false_prior,
        )
    if isinstance(value, list):
        return [
            _rewrite_json_strings(
                item,
                key=key,
                label=label,
                latest=latest,
                rewrite_false_prior=rewrite_false_prior,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            item_key: _rewrite_json_strings(
                item,
                key=key,
                label=label,
                latest=latest,
                rewrite_false_prior=rewrite_false_prior,
            )
            for item_key, item in value.items()
        }
    return value

def _strip_countdown_instruction_noise(value: Any, *, container_key: str = "") -> Any:
    if isinstance(value, str):
        return _strip_countdown_instruction_text(value)
    if isinstance(value, list):
        cleaned: list[Any] = []
        for item in value:
            if container_key == "rule_anchors" and isinstance(item, str):
                if "不超过不超过" in item:
                    continue
                cleaned.append(item)
                continue
            stripped = _strip_countdown_instruction_noise(item, container_key=container_key)
            if isinstance(stripped, str) and not stripped.strip():
                continue
            cleaned.append(stripped)
        return cleaned
    if isinstance(value, dict):
        return {
            item_key: _strip_countdown_instruction_noise(item, container_key=str(item_key))
            for item_key, item in value.items()
        }
    return value

def _strip_countdown_instruction_text(text: str) -> str:
    result = str(text or "")
    previous = None
    while previous != result:
        previous = result
        result = _COUNTDOWN_INSTRUCTION_RE.sub("", result)
    result = re.sub(r"\s{2,}", " ", result)
    result = re.sub(r"^\s*[。；;，,]\s*", "", result)
    return result.strip()

def _inspected_chapters(
    *,
    plans: list[ChapterPlan],
    band_rows: list[BandExperiencePlan] | None,
    current_chapter: int,
    include_current: bool,
) -> list[int]:
    inspected = [
        int(plan.chapter_number or 0)
        for plan in plans
        if include_current or int(plan.chapter_number or 0) > int(current_chapter or 0)
    ]
    for row in band_rows or []:
        for chapter in range(int(row.chapter_start or 0), int(row.chapter_end or 0) + 1):
            if (include_current or chapter > int(current_chapter or 0)) and chapter not in inspected:
                inspected.append(chapter)
    inspected.sort()
    return inspected

def _future_plan_prompt_payload(
    *,
    plans: list[ChapterPlan],
    canon_quality_context: dict[str, Any],
    obligations: list[NarrativeObligation],
    target_total_chapters: int,
    current_chapter: int,
    include_current: bool,
    band_rows: list[BandExperiencePlan] | None,
) -> dict[str, Any]:
    return {
        "writer_output": str(canon_quality_context.get("writer_output") or ""),
        "current_future_plan": [
            _chapter_plan_prompt_item(plan)
            for plan in plans
            if include_current or int(plan.chapter_number or 0) > int(current_chapter or 0)
        ],
        "canon_context": canon_quality_context.get("canon_context", []),
        "newly_extracted_facts": canon_quality_context.get("accepted_facts", []),
        "obligations": [obligation.model_dump(mode="json") for obligation in obligations],
        "target_total_chapters": int(target_total_chapters or 0),
        "band_context": [_band_prompt_item(row) for row in band_rows or []],
        "heuristic_hints": canon_quality_context.get("heuristic_hints", []),
    }

def _chapter_plan_prompt_item(plan: ChapterPlan) -> dict[str, Any]:
    return {
        "plan_item_id": str(plan.id or ""),
        "chapter_number": int(plan.chapter_number or 0),
        "title": str(plan.title or ""),
        "one_line": str(plan.one_line or ""),
        "goals": _loads(str(plan.goals_json or "[]"), []),
        "task_contract": _loads(str(plan.task_contract_json or "[]"), []),
        "experience_plan": _loads(str(plan.experience_plan_json or "{}"), {}),
        "status": str(plan.status or ""),
        "lock_level": "locked" if str(plan.status or "") == "accepted" else "soft",
    }

def _band_prompt_item(row: BandExperiencePlan) -> dict[str, Any]:
    return {
        "band_id": str(row.band_id or row.id or ""),
        "chapter_start": int(row.chapter_start or 0),
        "chapter_end": int(row.chapter_end or 0),
        "status": str(getattr(row, "status", "") or ""),
    }

def _first_prompt_target_plan(
    *,
    plans: list[ChapterPlan],
    current_chapter: int,
    include_current: bool,
) -> ChapterPlan | None:
    candidates = [
        plan
        for plan in plans
        if include_current or int(plan.chapter_number or 0) > int(current_chapter or 0)
    ]
    return sorted(candidates, key=lambda item: int(item.chapter_number or 0))[0] if candidates else None

def _prompt_issue_plan_id(
    *,
    raw_issue: dict[str, Any],
    impacts_by_id: dict[str, dict[str, Any]],
) -> str:
    for key in ("plan_item_id", "target_plan_id", "target_id"):
        value = str(raw_issue.get(key) or "").strip()
        if value:
            return value
    if len(impacts_by_id) == 1:
        return next(iter(impacts_by_id))
    for evidence in raw_issue.get("evidence", []) or []:
        if not isinstance(evidence, dict):
            continue
        location = str(evidence.get("location") or "")
        for plan_id in impacts_by_id:
            if plan_id and plan_id in location:
                return plan_id
    return ""

def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)

def _loads(raw: str, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return value if value is not None else default


__all__ = [
    name
    for name, value in globals().items()
    if name.startswith("_") and (callable(value) or name[1:].isupper())
]
