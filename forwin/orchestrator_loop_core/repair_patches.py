from __future__ import annotations

from forwin.orchestrator_loop_core.common import *


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
            schedule_json=json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False),
        )
    )
    structure_data = self._structure_data_from_row(arc_structure)
    for number in range(max(chapter_number, schedule.chapter_start), schedule.chapter_end + 1):
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
            experience_plan = self._current_chapter_repair_experience_plan(
                experience_plan,
                repair_instruction,
            )
        plan.experience_plan_json = json.dumps(experience_plan.model_dump(mode="json"), ensure_ascii=False)
        session.add(plan)


@classmethod
def _structure_data_from_row(cls, arc_structure: ArcStructureDraft | None):
    from forwin.orchestrator.phase24 import ArcStructureDraftData
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
        thread_priorities=json.loads(arc_structure.thread_priorities_json or "[]") or [],
        hotspot_candidates=json.loads(arc_structure.hotspot_candidates_json or "[]") or [],
        compression_candidates=json.loads(arc_structure.compression_candidates_json or "[]") or [],
        reader_promise=cls._reader_promise_from_row(arc_structure),
        arc_payoff_map=ArcPayoffMap.model_validate(json.loads(arc_structure.arc_payoff_map_json or "{}") or {}),
    )


@staticmethod
def _reader_promise_from_row(arc_structure: ArcStructureDraft):
    from forwin.protocol.experience import ReaderPromise

    return ReaderPromise.model_validate(json.loads(arc_structure.reader_promise_json or "{}") or {})


@staticmethod
def _current_chapter_repair_experience_plan(
    current_plan: ChapterExperiencePlan,
    repair_instruction: RepairInstruction,
) -> ChapterExperiencePlan:
    return current_plan.model_copy(
        update=WritingOrchestrator._chapter_experience_patch_payload(
            current_plan,
            repair_instruction,
        )
    )


@staticmethod
def _chapter_experience_patch_payload(
    current_plan: ChapterExperiencePlan,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    repair_rule_anchors = WritingOrchestrator._countdown_repair_rule_anchors(repair_instruction.must_fix)
    repair_rule_anchors.extend([
        f"repair must fix: {item}"
        for item in repair_instruction.must_fix[:3]
        if str(item or "").strip()
    ])
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
    if repair_instruction.failure_type == "hook_failure" and "hook_type" not in repair_instruction.design_patch:
        update["hook_type"] = "hard_cliffhanger"
    if repair_instruction.failure_type == "immersion" and not update["immersion_anchors"]:
        update["immersion_anchors"] = ["补入感官锚点", "让角色即时反应落地"]
    if repair_instruction.failure_type == "immersion" and not update["rule_anchors"]:
        update["rule_anchors"] = ["补清规则边界或代价，防止作者强行感"]
    if repair_rule_anchors:
        existing_rule_anchors = [str(item) for item in update.get("rule_anchors", []) or []]
        update["rule_anchors"] = [*repair_rule_anchors, *existing_rule_anchors]
    if repair_instruction.failure_type == "stall" and not update["progress_markers"]:
        update["progress_markers"] = ["让主目标出现不可逆推进"]
    if repair_instruction.failure_type == "stall" and not update["question_hook"]:
        update["question_hook"] = "补出一个比当前更强的新问题"
    return update


@staticmethod
def _countdown_repair_rule_anchors(must_fix: list[str]) -> list[str]:
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
        if not any(marker in item for marker in ("回升", "延长", "non_monotonic", "单调")):
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


@staticmethod
def _band_schedule_patch_payload(
    schedule: BandDelightSchedule,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    payload = schedule.model_dump(mode="json")
    payload.update(repair_instruction.design_patch)
    if repair_instruction.failure_type == "stall":
        payload["stall_guard_max_gap"] = 1
    if repair_instruction.failure_type == "immersion" and not payload.get("immersion_anchor_scene_goal"):
        payload["immersion_anchor_scene_goal"] = "每章都落一个可感知现场锚点"
    if repair_instruction.failure_type == "stall" and not payload.get("curiosity_beats"):
        payload["curiosity_beats"] = [
            {
                "chapter_hint": schedule.chapter_start,
                "question_open": "当前局面真正危险在哪里",
                "question_resolve": "先确认一个局部真相",
                "escalated_question": "更大的幕后压力是什么",
            }
        ]
    return payload


@staticmethod
def _arc_payoff_patch_payload(
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
    if repair_instruction.failure_type == "payoff_miss" and not payload.get("macro_payoffs"):
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
    if repair_instruction.failure_type == "immersion" and not payload.get("ambiguity_constraints"):
        payload["ambiguity_constraints"] = ["关键翻盘必须回指既有规则或线索。"]
    return payload


__all__ = [
    "_replace_band_schedule",
    "_structure_data_from_row",
    "_reader_promise_from_row",
    "_current_chapter_repair_experience_plan",
    "_chapter_experience_patch_payload",
    "_countdown_repair_rule_anchors",
    "_band_schedule_patch_payload",
    "_arc_payoff_patch_payload",
]
