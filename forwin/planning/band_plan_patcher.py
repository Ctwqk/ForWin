from __future__ import annotations

import json
import re
from typing import Any

from forwin.governance import PlanTaskItem, load_plan_task_contract, plan_task_contract_to_json
from forwin.models.base import new_id
from forwin.models.phase import BandExperiencePlan
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.protocol.experience import BandDelightSchedule, BandObligationContract


class BandPlanPatcher:
    def build_obligation_patch(
        self,
        *,
        project_id: str,
        band_row: BandExperiencePlan,
        obligations: list[NarrativeObligation],
        current_chapter: int,
        patch_type: str = "obligation_band_plan_binding",
    ) -> NarrativePlanPatch:
        affected = _future_band_chapters(
            chapter_start=int(band_row.chapter_start or 0),
            chapter_end=int(band_row.chapter_end or 0),
            current_chapter=int(current_chapter or 0),
        )
        source_ids = [item.id for item in obligations if item.id]
        contract = _contract_from_obligations(
            obligations=obligations,
            affected_chapters=affected,
            band_end=int(band_row.chapter_end or 0),
        )
        return NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type=patch_type,
            target_scope="band",
            target_arc_id=str(band_row.arc_id or ""),
            target_band_id=str(band_row.band_id or ""),
            affected_chapters=affected,
            source_obligation_ids=source_ids,
            old_contract=_band_contract_snapshot(band_row),
            new_contract={"band_obligation_contract": contract.model_dump(mode="json")},
            diff_summary=f"Bind {len(source_ids)} narrative obligation(s) to band plan {band_row.band_id}.",
            must_not_change=[f"remove unresolved obligation {item}" for item in source_ids],
            writer_context_injections=list(contract.writer_context_injections),
            reviewer_context_injections=list(contract.reviewer_context_injections),
            expected_resolution_tests=[
                test for test in contract.payoff_tests.values() if str(test).strip()
            ],
            validation_status="pending",
            metadata={
                "band_id": str(band_row.band_id or ""),
                "chapter_start": int(band_row.chapter_start or 0),
                "chapter_end": int(band_row.chapter_end or 0),
            },
        )

    def apply(
        self,
        row: BandExperiencePlan,
        patch: NarrativePlanPatch,
        *,
        obligations: list[NarrativeObligation],
    ) -> None:
        schedule = _load_schedule(row)
        existing = schedule.band_obligation_contract
        incoming = _incoming_contract(patch=patch, obligations=obligations, row=row)
        schedule.band_obligation_contract = _merge_contracts(existing, incoming)
        row.schedule_json = json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False)
        row.task_contract_json = plan_task_contract_to_json(
            _merge_task_contracts(
                load_plan_task_contract(row.task_contract_json),
                obligations=obligations,
            )
        )


def _load_schedule(row: BandExperiencePlan) -> BandDelightSchedule:
    try:
        payload = json.loads(row.schedule_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("band_id", str(row.band_id or ""))
    payload.setdefault("chapter_start", int(row.chapter_start or 0))
    payload.setdefault("chapter_end", int(row.chapter_end or 0))
    payload.setdefault("stall_guard_max_gap", int(row.stall_guard_max_gap or 0))
    return BandDelightSchedule.model_validate(payload)


def _incoming_contract(
    *,
    patch: NarrativePlanPatch,
    obligations: list[NarrativeObligation],
    row: BandExperiencePlan,
) -> BandObligationContract:
    raw = patch.new_contract.get("band_obligation_contract")
    if isinstance(raw, dict):
        return BandObligationContract.model_validate(raw)
    return _contract_from_obligations(
        obligations=obligations,
        affected_chapters=list(patch.affected_chapters or []),
        band_end=int(row.chapter_end or 0),
    )


def _contract_from_obligations(
    *,
    obligations: list[NarrativeObligation],
    affected_chapters: list[int],
    band_end: int,
) -> BandObligationContract:
    contract = BandObligationContract()
    for obligation in obligations:
        obligation_id = str(obligation.id or "").strip()
        if not obligation_id:
            continue
        _append_unique(contract.open_obligations, obligation_id)
        if obligation.priority in {"P0", "P1"} or int(obligation.deadline_chapter or 0) <= int(band_end or 0):
            _append_unique(contract.must_resolve_by_band_end, obligation_id)
        else:
            _append_unique(contract.allowed_carry_forward, obligation_id)
        if obligation.payoff_test:
            contract.payoff_tests[obligation_id] = obligation.payoff_test
        contract.affected_chapters[obligation_id] = list(affected_chapters)
        writer_injection = {
            "type": "narrative_obligation",
            "scope": "band",
            "obligation_id": obligation_id,
            "priority": obligation.priority,
            "summary": obligation.summary,
            "deadline_chapter": obligation.deadline_chapter,
            "payoff_test": obligation.payoff_test,
        }
        reviewer_injection = {
            "type": "narrative_obligation",
            "scope": "band",
            "obligation_id": obligation_id,
            "deadline_chapter": obligation.deadline_chapter,
            "payoff_test": obligation.payoff_test,
        }
        _append_dict_unique(contract.writer_context_injections, writer_injection, key="obligation_id")
        _append_dict_unique(contract.reviewer_context_injections, reviewer_injection, key="obligation_id")
    return contract


def _merge_contracts(existing: BandObligationContract, incoming: BandObligationContract) -> BandObligationContract:
    result = existing.model_copy(deep=True)
    for field in ("open_obligations", "must_resolve_by_band_end", "allowed_carry_forward"):
        target = getattr(result, field)
        for obligation_id in getattr(incoming, field):
            _append_unique(target, obligation_id)
    result.payoff_tests.update(incoming.payoff_tests)
    result.affected_chapters.update(incoming.affected_chapters)
    for item in incoming.writer_context_injections:
        _append_dict_unique(result.writer_context_injections, item, key="obligation_id")
    for item in incoming.reviewer_context_injections:
        _append_dict_unique(result.reviewer_context_injections, item, key="obligation_id")
    return result


def _merge_task_contracts(
    existing: list[PlanTaskItem],
    *,
    obligations: list[NarrativeObligation],
) -> list[PlanTaskItem]:
    result = [
        item for item in existing
        if not (
            str(item.source or "") == "narrative_obligation"
            and any(_task_matches_obligation(item, obligation) for obligation in obligations)
        )
    ]
    for obligation in obligations:
        if not obligation.id:
            continue
        result.append(
            PlanTaskItem(
                task_type="plot_advance",
                description=obligation.payoff_test or obligation.summary,
                target_name=obligation.id,
                required_keywords=_required_keywords(obligation.payoff_test or obligation.summary),
                source="narrative_obligation",
            )
        )
    return result


def _task_matches_obligation(task: PlanTaskItem, obligation: NarrativeObligation) -> bool:
    obligation_id = str(obligation.id or "")
    return bool(obligation_id and (task.target_name == obligation_id or obligation_id in task.description))


def _required_keywords(text: str) -> list[str]:
    cleaned = re.sub(r"^第\d+章[前内]?(必须|需要)?", "", str(text or "").strip("。；; "))
    cleaned = re.sub(r"^(必须|需要|给出|兑现|解释|补足)+", "", cleaned)
    if "给出" in cleaned:
        cleaned = cleaned.split("给出", 1)[1]
    parts = [
        part.strip(" ，,。；;：:")
        for part in re.split(r"的|、|，|,|；|;", cleaned)
        if len(part.strip(" ，,。；;：:")) >= 2
    ]
    return list(dict.fromkeys(parts[:3]))


def _future_band_chapters(*, chapter_start: int, chapter_end: int, current_chapter: int) -> list[int]:
    start = max(int(chapter_start or 0), int(current_chapter or 0) + 1)
    end = int(chapter_end or 0)
    if start <= 0 or end < start:
        return []
    return list(range(start, end + 1))


def _band_contract_snapshot(row: BandExperiencePlan) -> dict[str, Any]:
    schedule = _load_schedule(row)
    return {
        "band_id": str(row.band_id or ""),
        "chapter_start": int(row.chapter_start or 0),
        "chapter_end": int(row.chapter_end or 0),
        "band_obligation_contract": schedule.band_obligation_contract.model_dump(mode="json"),
        "task_contract": [
            item.model_dump(mode="json") for item in load_plan_task_contract(row.task_contract_json)
        ],
    }


def _append_unique(target: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in target:
        target.append(text)


def _append_dict_unique(target: list[dict[str, Any]], value: dict[str, Any], *, key: str) -> None:
    marker = str(value.get(key) or "").strip()
    if marker:
        target[:] = [item for item in target if str(item.get(key) or "").strip() != marker]
    target.append(value)
