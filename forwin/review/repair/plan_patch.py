"""Finite repair plan changes; accepted plan protection and transaction boundaries stay unchanged."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from forwin.models import new_id
from forwin.models.phase import ArcStructureDraft, BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.planning.arc_envelope import ArcEnvelopeManager
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import (
    ArcPayoffMap,
    BandDelightSchedule,
    ChapterExperiencePlan,
)
from forwin.protocol.review import RepairInstruction
from forwin.retrieval import RetrievalBroker
from forwin.review.results import load_json_list
from forwin.state.repo import StateRepository


@dataclass(frozen=True, slots=True)
class RepairPlanPatchRequest:
    session: Session
    repo: StateRepository
    project_id: str
    chapter_plan: ChapterPlan
    context: ChapterContextPack
    repair_scope: str
    instruction: RepairInstruction


@dataclass(frozen=True, slots=True)
class RepairPlanPatchResult:
    design_patch: dict[str, object]
    context: ChapterContextPack
    chapter_snapshot: dict[str, object]
    band_snapshot: dict[str, object]
    failure_reason: str = ""


def reader_promise_from_row(arc_structure: ArcStructureDraft):
    from forwin.protocol.experience import ReaderPromise

    return ReaderPromise.model_validate(
        json.loads(arc_structure.reader_promise_json or "{}") or {}
    )


def current_chapter_repair_experience_plan(
    current_plan: ChapterExperiencePlan,
    repair_instruction: RepairInstruction,
) -> ChapterExperiencePlan:
    return current_plan.model_copy(
        update=chapter_experience_patch_payload(
            current_plan,
            repair_instruction,
        )
    )


def chapter_experience_patch_payload(
    current_plan: ChapterExperiencePlan,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    repair_rule_anchors = countdown_repair_rule_anchors(repair_instruction.must_fix)
    update: dict[str, object] = {
        "planned_reward_tags": list(
            repair_instruction.design_patch.get("planned_reward_tags")
            or current_plan.planned_reward_tags
            or ["mystery"]
        ),
        "selected_template_ids": list(
            repair_instruction.design_patch.get("selected_template_ids")
            or current_plan.selected_template_ids
        ),
        "hook_type": str(
            repair_instruction.design_patch.get("hook_type")
            or current_plan.hook_type
            or "cliffhanger_question"
        ),
        "question_hook": str(
            repair_instruction.design_patch.get("question_hook")
            or current_plan.question_hook
        ),
        "question_resolution": str(
            repair_instruction.design_patch.get("question_resolution")
            or current_plan.question_resolution
        ),
        "immersion_anchors": list(
            repair_instruction.design_patch.get("immersion_anchors")
            or current_plan.immersion_anchors
        ),
        "progress_markers": list(
            repair_instruction.design_patch.get("progress_markers")
            or current_plan.progress_markers
        ),
        "rule_anchors": list(
            repair_instruction.design_patch.get("rule_anchors")
            or current_plan.rule_anchors
        ),
        "relationship_or_status_shift": str(
            repair_instruction.design_patch.get("relationship_or_status_shift")
            or current_plan.relationship_or_status_shift
        ),
        "minimum_progress_channels": list(
            repair_instruction.design_patch.get("minimum_progress_channels")
            or current_plan.minimum_progress_channels
        ),
    }
    if (
        repair_instruction.failure_type == "hook_failure"
        and "hook_type" not in repair_instruction.design_patch
    ):
        update["hook_type"] = "hard_cliffhanger"
    if (
        repair_instruction.failure_type == "immersion"
        and not update["immersion_anchors"]
    ):
        update["immersion_anchors"] = ["补入感官锚点", "让角色即时反应落地"]
    if repair_instruction.failure_type == "immersion" and not update["rule_anchors"]:
        update["rule_anchors"] = ["补清规则边界或代价，防止作者强行感"]
    # Generic repair text belongs to this rewrite, not the persistent plan.
    existing_rule_anchors = [
        str(item) for item in update.get("rule_anchors", []) or []
        if not str(item).startswith("repair must fix:")
    ]
    update["rule_anchors"] = [*repair_rule_anchors, *existing_rule_anchors]
    if repair_instruction.failure_type == "stall" and not update["progress_markers"]:
        update["progress_markers"] = ["让主目标出现不可逆推进"]
    if repair_instruction.failure_type == "stall" and not update["question_hook"]:
        update["question_hook"] = "补出一个比当前更强的新问题"
    return update


def countdown_repair_rule_anchors(must_fix: list[str]) -> list[str]:
    anchors: list[str] = []
    for raw in must_fix:
        item = str(raw or "").strip()
        if not item:
            continue
        if "倒计时" not in item:
            continue
        stale_match = re.search(
            r"回溯旧倒计时为\s*([^，。,；;]+).*?([0-9]+)\s*分钟级别",
            item,
        )
        if stale_match:
            raw_target = str(stale_match.group(1) or "").strip()
            latest = int(stale_match.group(2))
            anchors.append(
                "repair countdown hard constraint: 旧计划/旧摘要时间不得写成前文事实；"
                f"{raw_target}必须删除，或明确改成公开伪数据/误导信息，"
                f"同一记忆重置周期只能写小于等于{latest}分钟。"
                "不得写“系统日志原本还有三天/七天/几小时”来解释当前倒计时。"
            )
            continue
        if not any(
            marker in item for marker in ("回升", "延长", "non_monotonic", "单调")
        ):
            continue
        match = re.search(r"从\s*([0-9]+)\s*分钟(?:回升|延长)到\s*([^，。,；;]+)", item)
        if match:
            previous = int(match.group(1))
            raw_target = str(match.group(2) or "").strip()
            target_digit = re.search(r"([0-9]+)\s*分钟", raw_target)
            target_constraint = (
                f"{int(target_digit.group(1))}分钟必须改成小于等于{previous}分钟"
                if target_digit
                else f"{raw_target}必须删除或改为小于等于{previous}分钟"
            )
            anchors.append(
                "repair countdown hard constraint: 同一倒计时 ledger 在本章全文必须单调减少；"
                f"{target_constraint}，"
                "并同步修正文中所有相关倒计时、角色判断和摘要。除非正文明确 reset 或 branch clock，"
                "不得在更小剩余时间之后再写更大的剩余时间。"
            )
            continue
        anchors.append(
            "repair countdown hard constraint: 同一倒计时 ledger 在本章全文必须单调减少；"
            "重写前先列出正文所有剩余时间，按出现顺序改成不增加序列。除非正文明确 reset 或 branch clock，"
            "不得在更小剩余时间之后再写更大的剩余时间。"
        )
    return anchors


def band_schedule_patch_payload(
    schedule: BandDelightSchedule,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    payload = schedule.model_dump(mode="json")
    payload.update(repair_instruction.design_patch)
    if repair_instruction.failure_type == "stall":
        payload["stall_guard_max_gap"] = 1
    if repair_instruction.failure_type == "immersion" and not payload.get(
        "immersion_anchor_scene_goal"
    ):
        payload["immersion_anchor_scene_goal"] = "每章都落一个可感知现场锚点"
    if repair_instruction.failure_type == "stall" and not payload.get(
        "curiosity_beats"
    ):
        payload["curiosity_beats"] = [
            {
                "chapter_hint": schedule.chapter_start,
                "question_open": "当前局面真正危险在哪里",
                "question_resolve": "先确认一个局部真相",
                "escalated_question": "更大的幕后压力是什么",
            }
        ]
    return payload


def arc_payoff_patch_payload(
    payoff_map: ArcPayoffMap,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    payload = payoff_map.model_dump(mode="json")
    patch = dict(repair_instruction.design_patch)
    if "macro_payoffs" in patch:
        payload["macro_payoffs"] = patch["macro_payoffs"]
    if "awe_kit" in patch:
        payload["awe_kit"] = patch["awe_kit"]
    if "revelation_layers" in patch:
        payload["revelation_layers"] = patch["revelation_layers"]
    if "ambiguity_constraints" in patch:
        payload["ambiguity_constraints"] = patch["ambiguity_constraints"]
    if repair_instruction.failure_type == "payoff_miss" and not payload.get(
        "macro_payoffs"
    ):
        payload["macro_payoffs"] = [
            {
                "payoff_id": "repair-payoff-1",
                "category": "mystery",
                "template_id": "mystery-locked-clue",
                "target_chapter_hint": "near-term",
                "setup_requirement": "缩短 setup 到本 band 内",
                "success_signal": "读者感到明确回报已经到账",
            }
        ]
    if repair_instruction.failure_type == "immersion" and not payload.get(
        "ambiguity_constraints"
    ):
        payload["ambiguity_constraints"] = ["关键翻盘必须回指既有规则或线索。"]
    return payload


def arc_structure_data_from_row(arc_structure: ArcStructureDraft | None):
    from forwin.planning.arc_envelope import ArcStructureDraftData
    from forwin.protocol.experience import ReaderPromise

    if arc_structure is None:
        return ArcStructureDraftData(
            phase_layout=[],
            key_beats=[],
            thread_priorities=[],
            hotspot_candidates=[],
            compression_candidates=[],
            reader_promise=ReaderPromise(),
            arc_payoff_map=ArcPayoffMap(),
        )
    return ArcStructureDraftData(
        phase_layout=json.loads(arc_structure.phase_layout_json or "[]") or [],
        key_beats=json.loads(arc_structure.key_beats_json or "[]") or [],
        thread_priorities=json.loads(arc_structure.thread_priorities_json or "[]")
        or [],
        hotspot_candidates=json.loads(arc_structure.hotspot_candidates_json or "[]")
        or [],
        compression_candidates=json.loads(
            arc_structure.compression_candidates_json or "[]"
        )
        or [],
        reader_promise=reader_promise_from_row(arc_structure),
        arc_payoff_map=ArcPayoffMap.model_validate(
            json.loads(arc_structure.arc_payoff_map_json or "{}") or {}
        ),
    )


def chapter_plan_snapshot(
    *,
    repo: StateRepository,
    project_id: str,
    chapter_plan: ChapterPlan,
    experience_plan: ChapterExperiencePlan | None = None,
    transient_overlay: bool = False,
) -> dict[str, object]:
    live_experience_plan = experience_plan or repo.get_chapter_experience_plan(
        project_id,
        chapter_plan.chapter_number,
    )
    return {
        "chapter_number": int(chapter_plan.chapter_number or 0),
        "title": str(chapter_plan.title or ""),
        "one_line": str(chapter_plan.one_line or ""),
        "goals": load_json_list(getattr(chapter_plan, "goals_json", "[]")),
        "task_contract": load_json_list(
            getattr(chapter_plan, "task_contract_json", "[]")
        ),
        "experience_plan": (
            live_experience_plan.model_dump(mode="json")
            if live_experience_plan is not None
            else {}
        ),
        "transient_overlay": bool(transient_overlay),
    }


def band_plan_snapshot(
    *,
    repo: StateRepository,
    project_id: str,
    chapter_number: int,
    schedule: BandDelightSchedule | None = None,
    transient_overlay: bool = False,
) -> dict[str, object]:
    row = repo.get_band_row_for_chapter(project_id, chapter_number)
    live_schedule = schedule or repo.get_band_experience_plan_for_chapter(
        project_id, chapter_number
    )
    if row is None and live_schedule is None:
        return {}
    return {
        "band_id": str(
            getattr(row, "band_id", getattr(live_schedule, "band_id", "")) or ""
        ),
        "chapter_start": int(
            getattr(row, "chapter_start", getattr(live_schedule, "chapter_start", 0))
            or 0
        ),
        "chapter_end": int(
            getattr(row, "chapter_end", getattr(live_schedule, "chapter_end", 0)) or 0
        ),
        "task_contract": load_json_list(getattr(row, "task_contract_json", "[]")),
        "schedule": live_schedule.model_dump(mode="json")
        if live_schedule is not None
        else {},
        "transient_overlay": bool(transient_overlay),
    }


class RepairPlanPatchService:
    def __init__(
        self,
        *,
        retrieval_broker: RetrievalBroker,
        arc_envelope_manager: ArcEnvelopeManager,
    ):
        self.retrieval_broker = retrieval_broker
        self.arc_envelope_manager = arc_envelope_manager

    def apply(self, request: RepairPlanPatchRequest) -> RepairPlanPatchResult:
        if request.context.canon_read_baseline is not None:
            request.context.canon_read_baseline.assert_current(request.session)
        result = self._apply_plan_patch(request)
        return replace(result, context=self.retrieval_broker.prepare_repair_context(
            result.context, request.instruction
        ))

    def _apply_plan_patch(self, request: RepairPlanPatchRequest) -> RepairPlanPatchResult:
        session = request.session
        repo = request.repo
        project_id = request.project_id
        chapter_plan = request.chapter_plan
        context = request.context
        repair_scope = request.repair_scope
        repair_instruction = request.instruction
        if getattr(chapter_plan, "active_commit_id", None) and repair_scope != "draft":
            raise ValueError(
                "accepted chapter plan requires an isolated candidate revision"
            )
        current_plan = (
            repo.get_chapter_experience_plan(project_id, chapter_plan.chapter_number)
            or ChapterExperiencePlan()
        )
        band_schedule = repo.get_band_experience_plan_for_chapter(
            project_id, chapter_plan.chapter_number
        )
        arc_structure = repo.get_latest_arc_structure_draft(project_id)
        patch = dict(repair_instruction.design_patch)
        patch["repair_scope"] = repair_scope

        if repair_scope == "draft":
            updated_plan = current_plan.model_copy(
                update=chapter_experience_patch_payload(
                    current_plan, repair_instruction
                )
            )
            updated_context = self._with_transient_experience_plan(
                repo, context, updated_plan
            )
            return RepairPlanPatchResult(
                updated_plan.model_dump(mode="json"),
                updated_context,
                chapter_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    experience_plan=updated_plan,
                    transient_overlay=True,
                ),
                band_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    schedule=band_schedule,
                    transient_overlay=True,
                ),
                "",
            )

        if repair_scope == "chapter_plan":
            updated_plan = current_plan.model_copy(
                update=chapter_experience_patch_payload(
                    current_plan, repair_instruction
                )
            )
            chapter_plan.experience_plan_json = json.dumps(
                updated_plan.model_dump(mode="json"),
                ensure_ascii=False,
            )
            if str(patch.get("chapter_plan_title") or patch.get("title") or "").strip():
                chapter_plan.title = str(
                    patch.get("chapter_plan_title") or patch.get("title") or ""
                ).strip()
            if str(
                patch.get("chapter_plan_one_line") or patch.get("one_line") or ""
            ).strip():
                chapter_plan.one_line = str(
                    patch.get("chapter_plan_one_line") or patch.get("one_line") or ""
                ).strip()
            goal_patch = patch.get("chapter_goals")
            if not isinstance(goal_patch, list):
                goal_patch = patch.get("goals")
            if isinstance(goal_patch, list):
                chapter_plan.goals_json = json.dumps(goal_patch, ensure_ascii=False)
            task_contract_patch = patch.get("chapter_task_contract")
            if not isinstance(task_contract_patch, list):
                task_contract_patch = patch.get("task_contract")
            if isinstance(task_contract_patch, list):
                chapter_plan.task_contract_json = json.dumps(
                    task_contract_patch, ensure_ascii=False
                )
            session.add(chapter_plan)
            session.flush()
            return RepairPlanPatchResult(
                updated_plan.model_dump(mode="json"),
                self.retrieval_broker.build_chapter_context(
                    repo, project_id, chapter_plan,
                    **({"baseline": context.canon_read_baseline} if context.canon_read_baseline is not None else {}),
                ),
                chapter_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                ),
                band_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                ),
                "",
            )

        if band_schedule is not None:
            updated_schedule = BandDelightSchedule.model_validate(
                band_schedule_patch_payload(band_schedule, repair_instruction)
            )
            self._replace_band_schedule(
                session=session,
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                schedule=updated_schedule,
                arc_structure=arc_structure,
                repair_instruction=repair_instruction,
            )
            session.flush()
            return RepairPlanPatchResult(
                updated_schedule.model_dump(mode="json"),
                self.retrieval_broker.build_chapter_context(
                    repo, project_id, chapter_plan,
                    **({"baseline": context.canon_read_baseline} if context.canon_read_baseline is not None else {}),
                ),
                chapter_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                ),
                band_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                ),
                "",
            )

        updated_plan = current_plan.model_copy(
            update=chapter_experience_patch_payload(current_plan, repair_instruction)
        )
        updated_context = self._with_transient_experience_plan(
            repo, context, updated_plan
        )
        return RepairPlanPatchResult(
            updated_plan.model_dump(mode="json"),
            updated_context,
            chapter_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
                experience_plan=updated_plan,
                transient_overlay=True,
            ),
            band_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                schedule=band_schedule,
                transient_overlay=True,
            ),
            "",
        )

    def _with_transient_experience_plan(
        self,
        repo: StateRepository,
        context: ChapterContextPack,
        updated_plan: ChapterExperiencePlan,
    ) -> ChapterContextPack:
        updated_context = context.model_copy(
            update={"chapter_experience_plan": updated_plan}
        )
        if context.canon_read_baseline is not None:
            # RepairService commits before apply(). Rebind accepted reads in its
            # current transaction, preserving the original fence and authored overlay.
            updated_context = self.retrieval_broker.hydrate_required_context(
                repo, updated_context, trim=False
            )
        return updated_context

    def _replace_band_schedule(
        self,
        *,
        session: Session,
        repo: StateRepository,
        project_id: str,
        chapter_number: int,
        schedule: BandDelightSchedule,
        arc_structure: ArcStructureDraft | None,
        repair_instruction: RepairInstruction | None = None,
    ) -> None:
        active_arc = repo.get_active_arc_plan(project_id)
        if active_arc is None:
            return
        session.query(BandExperiencePlan).filter(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.arc_id == active_arc.id,
            BandExperiencePlan.band_id == schedule.band_id,
        ).delete(synchronize_session=False)
        session.add(
            BandExperiencePlan(
                id=new_id(),
                project_id=project_id,
                arc_id=active_arc.id,
                band_id=schedule.band_id,
                chapter_start=schedule.chapter_start,
                chapter_end=schedule.chapter_end,
                stall_guard_max_gap=schedule.stall_guard_max_gap,
                schedule_json=json.dumps(
                    schedule.model_dump(mode="json"), ensure_ascii=False
                ),
            )
        )
        structure_data = arc_structure_data_from_row(arc_structure)
        for number in range(
            max(chapter_number, schedule.chapter_start), schedule.chapter_end + 1
        ):
            plan = repo.get_chapter_plan(project_id, number)
            if plan is None:
                continue
            experience_plan = self.arc_envelope_manager._derive_chapter_experience_plan(
                chapter_number=number,
                structure=structure_data,
                schedule=schedule,
                chapter_plan=plan,
            )
            if number == chapter_number and repair_instruction is not None:
                experience_plan = current_chapter_repair_experience_plan(
                    experience_plan,
                    repair_instruction,
                )
            plan.experience_plan_json = json.dumps(
                experience_plan.model_dump(mode="json"), ensure_ascii=False
            )
            session.add(plan)
