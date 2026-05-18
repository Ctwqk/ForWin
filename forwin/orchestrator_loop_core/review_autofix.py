from __future__ import annotations

from forwin.orchestrator_loop_core.common import *

def _persist_draft_and_review(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    chapter_plan: ChapterPlan,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    review: ReviewVerdict,
) -> tuple[WriterOutput, ChapterDraft, ChapterReview]:
    artifact_paths = self.artifact_store.save_writer_output(
        project_id=project_id,
        chapter_number=chapter_number,
        writer_output=writer_output,
    )
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.WRITER_OUTPUT_ARTIFACT_SAVED,
        scope="chapter",
        summary=f"第{chapter_number}章 writer output artifact 已保存。",
        payload={
            "draft_blob_path": artifact_paths.get("draft_blob_path", ""),
            "artifact_meta_path": artifact_paths.get("meta_path", ""),
            "char_count": int(getattr(writer_output, "char_count", 0) or 0),
        },
    )
    persisted_output = artifact_paths["writer_output"].model_copy(
        update={
            "generation_meta": {
                **writer_output.generation_meta,
                "artifact_meta_path": artifact_paths["meta_path"],
            },
        }
    )
    draft = updater.save_draft(
        chapter_plan_id=chapter_plan.id,
        writer_output=persisted_output,
        raw_response=artifact_paths["meta_path"],
        model_name=self.config.minimax_model,
    )
    review_row = updater.save_review(draft.id, review)
    repair_attempt_count = session.query(ChapterRewriteAttempt).filter(
        ChapterRewriteAttempt.project_id == project_id,
        ChapterRewriteAttempt.chapter_number == chapter_number,
    ).count()
    CandidateDraftRepository(session).upsert_from_review(
        project_id=project_id,
        chapter_plan=chapter_plan,
        draft=draft,
        review=review_row,
        writer_output=persisted_output,
        repair_attempt_count=repair_attempt_count,
    )
    updater.mark_chapter_status(project_id, chapter_number, "drafted")
    session.flush()
    return persisted_output, draft, review_row

def _review_current_output(
    self,
    *,
    repo: StateRepository,
    checker: ContinuityChecker,
    project_id: str,
    context,
    writer_output: WriterOutput,
) -> ReviewVerdict:
    reviewer_skill_layers = self._select_skill_layers(
        scope="reviewer",
        stage_key="chapter_review",
        task_family="review_chapter",
    )
    return self._call_with_compatible_kwargs(
        self.review_hub.review,
        project_id=project_id,
        repo=repo,
        context=context,
        writer_output=writer_output,
        continuity_checker=checker,
        reviewer_skill_layers=reviewer_skill_layers,
    )

@staticmethod
def _apply_canon_name_drift_autofix(
    writer_output: WriterOutput,
    review: ReviewVerdict,
) -> WriterOutput | None:
    replacements: dict[str, str] = {}
    for issue in review.issues:
        if str(issue.rule_name or "") != "canon_name_drift":
            continue
        if str(issue.severity or "") != "error":
            continue
        entity_names = list(issue.entity_names or [])
        if len(entity_names) < 2:
            continue
        observed = str(entity_names[0] or "").strip()
        canonical = str(entity_names[1] or "").strip()
        if not observed or not canonical or observed == canonical:
            continue
        if observed.startswith(canonical):
            continue
        if not is_plausible_person_name(observed) or not is_plausible_person_name(canonical):
            continue
        replacements[observed] = canonical

    if not replacements:
        return None

    payload = WritingOrchestrator._replace_canon_name_strings(
        writer_output.model_dump(mode="python"),
        replacements,
    )
    payload["char_count"] = len(str(payload.get("body") or ""))
    generation_meta = dict(payload.get("generation_meta") or {})
    previous_autofix = generation_meta.get("canon_name_autofix")
    if isinstance(previous_autofix, dict):
        autofix_meta = {str(key): str(value) for key, value in previous_autofix.items()}
        autofix_meta.update(replacements)
    else:
        autofix_meta = replacements
    generation_meta["canon_name_autofix"] = autofix_meta
    payload["generation_meta"] = generation_meta
    return WriterOutput.model_validate(payload)

@staticmethod
def _apply_subworld_admission_autofix(
    writer_output: WriterOutput,
    review: ReviewVerdict,
    *,
    protected_names: set[str] | None = None,
) -> WriterOutput | None:
    replacements: dict[str, str] = {}
    body = str(writer_output.body or "")
    protected = {
        ContinuityChecker._normalize_character_reference(name)
        for name in (protected_names or set())
        if str(name or "").strip()
    }
    for issue in review.issues:
        if str(issue.rule_name or "") != "sub_world_unknown_named_entity":
            continue
        if str(issue.severity or "") != "error":
            continue
        entity_names = list(issue.entity_names or [])
        if not entity_names:
            continue
        observed = str(entity_names[0] or "").strip()
        normalized_observed = ContinuityChecker._normalize_character_reference(observed)
        if not observed or not WritingOrchestrator._looks_like_genericizable_unknown_reference(normalized_observed):
            continue
        if normalized_observed in protected:
            continue
        generic = WritingOrchestrator._generic_subworld_reference(body, observed)
        replacements[observed] = generic
        if len(observed) >= 2:
            replacements[f"{observed[0]}总"] = generic
        for title in WritingOrchestrator._subworld_role_titles():
            phrase = f"{title}{observed}"
            if phrase in body:
                replacements[phrase] = title

    if not replacements:
        return None

    payload = WritingOrchestrator._replace_canon_name_strings(
        writer_output.model_dump(mode="python"),
        replacements,
    )
    payload["char_count"] = len(str(payload.get("body") or ""))
    generation_meta = dict(payload.get("generation_meta") or {})
    previous_autofix = generation_meta.get("subworld_admission_autofix")
    if isinstance(previous_autofix, dict):
        autofix_meta = {str(key): str(value) for key, value in previous_autofix.items()}
        autofix_meta.update(replacements)
    else:
        autofix_meta = replacements
    generation_meta["subworld_admission_autofix"] = autofix_meta
    payload["generation_meta"] = generation_meta
    return WriterOutput.model_validate(payload)

@staticmethod
def _apply_placeholder_leakage_autofix(
    writer_output: WriterOutput,
    review: ReviewVerdict,
) -> WriterOutput | None:
    body = str(writer_output.body or "")
    if "工作人员" not in body and "工作人员" not in str(writer_output.end_of_chapter_summary or ""):
        return None
    should_replace = any(
        str(issue.rule_name or "") == "bare_role_placeholder_leakage"
        and str(issue.severity or "") == "error"
        for issue in review.issues
    )
    if not should_replace:
        return None
    replacement = WritingOrchestrator._placeholder_role_replacement(body)
    replacements = {"工作人员": replacement}
    payload = WritingOrchestrator._replace_canon_name_strings(
        writer_output.model_dump(mode="python"),
        replacements,
    )
    payload["char_count"] = len(str(payload.get("body") or ""))
    generation_meta = dict(payload.get("generation_meta") or {})
    previous_autofix = generation_meta.get("placeholder_leakage_autofix")
    if isinstance(previous_autofix, dict):
        autofix_meta = {str(key): str(value) for key, value in previous_autofix.items()}
        autofix_meta.update(replacements)
    else:
        autofix_meta = replacements
    generation_meta["placeholder_leakage_autofix"] = autofix_meta
    payload["generation_meta"] = generation_meta
    return WriterOutput.model_validate(payload)

@staticmethod
def _placeholder_role_replacement(body: str) -> str:
    text = str(body or "")
    if "旧书摊" in text or "书摊" in text:
        return "旧书摊主"
    if "系统维护组" in text or "维护组" in text:
        return "系统维护员"
    if "分馆" in text or "地下三层" in text:
        return "地下分馆管理员"
    return "具体见证人"

@staticmethod
def _looks_like_genericizable_unknown_reference(name: str) -> bool:
    text = ContinuityChecker._normalize_character_reference(name)
    if not text:
        return False
    if is_plausible_person_name(text):
        return True
    if 2 <= len(text) <= 3 and text[0] in {"老", "小", "阿"}:
        return all("\u4e00" <= char <= "\u9fff" for char in text[1:])
    return False

@staticmethod
def _project_character_names(repo: StateRepository, project_id: str) -> set[str]:
    names: set[str] = set()
    try:
        project = repo.get_project(project_id)
    except Exception:  # noqa: BLE001
        project = None
    if project is not None:
        names.update(
            extract_expected_protagonist_names(
                str(getattr(project, "premise", "") or ""),
                str(getattr(project, "setting_summary", "") or ""),
            )
        )
    try:
        entities = repo.get_active_entities(project_id)
    except Exception:  # noqa: BLE001
        return names
    for entity in entities or []:
        if str(getattr(entity, "kind", "") or "") != "character":
            continue
        raw_names = [getattr(entity, "name", "") or "", *(getattr(entity, "aliases", []) or [])]
        for raw_name in raw_names:
            name = ContinuityChecker._normalize_character_reference(str(raw_name or ""))
            if name:
                names.add(name)
    return names

@staticmethod
def _generic_subworld_reference(body: str, observed: str) -> str:
    if observed in body:
        index = body.find(observed)
        marker_window = body[max(0, index - 30) : index + len(observed) + 30]
    else:
        marker_window = body
    if any(marker in marker_window for marker in ("集团", "董事", "会议", "总监", "高管", "部门")):
        return "集团高管"
    return "馆员"

@staticmethod
def _subworld_role_titles() -> tuple[str, ...]:
    return (
        "首席运营官",
        "运营负责人",
        "财务总监",
        "财务负责人",
        "法务部负责人",
        "法务负责人",
        "部门总监",
        "部门负责人",
        "集团董事",
        "董事会成员",
        "安全主管",
        "安保主管",
        "项目负责人",
    )

@staticmethod
def _replace_canon_name_strings(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for observed, canonical in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            result = result.replace(observed, canonical)
        return result
    if isinstance(value, list):
        return [
            WritingOrchestrator._replace_canon_name_strings(item, replacements)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            WritingOrchestrator._replace_canon_name_strings(item, replacements)
            for item in value
        )
    if isinstance(value, dict):
        return {
            (
                WritingOrchestrator._replace_canon_name_strings(key, replacements)
                if isinstance(key, str)
                else key
            ): WritingOrchestrator._replace_canon_name_strings(item, replacements)
            for key, item in value.items()
        }
    return value

@staticmethod
def _review_event_payload(review: ReviewVerdict) -> dict[str, object]:
    return {
        "verdict": review.verdict,
        "issue_types": [
            str(getattr(issue, "issue_type", getattr(issue, "rule_name", "")) or "")
            for issue in review.issues
        ],
        "issue_groups": [
            str(getattr(issue, "issue_group", "") or issue_group_for_issue(
                issue_type=str(getattr(issue, "issue_type", "") or ""),
                rule_name=str(getattr(issue, "rule_name", "") or ""),
            ))
            for issue in review.issues
        ],
        "forced_accept_applied": bool(review.forced_accept_applied),
    }

@staticmethod
def _review_issue_payloads(review: ReviewVerdict) -> list[dict[str, object]]:
    issues = review.residual_review_issues or review.issues
    return [issue.model_dump(mode="json") for issue in issues]

def _record_map_movement_review_issues(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    review: ReviewVerdict,
    parent_event_id: str = "",
) -> None:
    issues = [
        issue
        for issue in review.issues
        if str(getattr(issue, "rule_name", "") or "").startswith("map_")
    ]
    if not issues:
        return
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.MAP_MOVEMENT_REVIEW_ISSUE,
        scope="chapter",
        summary=f"第{chapter_number}章 map movement reviewer 发现 {len(issues)} 个问题。",
        payload=audit_payload(
            stage="map_movement_review",
            status="issue",
            operation_id=self._audit_operation_id(),
            issue_count=len(issues),
            issues=[
                {
                    "rule_name": str(issue.rule_name or ""),
                    "issue_type": str(issue.issue_type or ""),
                    "severity": str(issue.severity or ""),
                    "issue_group": str(issue.issue_group or ""),
                    "target_scope": str(issue.target_scope or ""),
                    "entity_names": list(issue.entity_names or []),
                    "evidence_refs": list(issue.evidence_refs or []),
                }
                for issue in issues
            ],
        ),
        parent_event_id=parent_event_id,
    )

@staticmethod
def _review_canon_risk(review: ReviewVerdict) -> str:
    if review.final_gate_decision is not None:
        return str(review.final_gate_decision.canon_risk or "")
    if review.forced_accept_applied:
        return "low"
    if review.verdict == "fail":
        return "high"
    return ""

@staticmethod
def _load_json_list(raw: str) -> list[object]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []
    return payload if isinstance(payload, list) else []

def _chapter_plan_snapshot(
    self,
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
        "goals": self._load_json_list(getattr(chapter_plan, "goals_json", "[]")),
        "task_contract": self._load_json_list(getattr(chapter_plan, "task_contract_json", "[]")),
        "experience_plan": (
            live_experience_plan.model_dump(mode="json")
            if live_experience_plan is not None
            else {}
        ),
        "transient_overlay": bool(transient_overlay),
    }

def _band_plan_snapshot(
    self,
    *,
    repo: StateRepository,
    project_id: str,
    chapter_number: int,
    schedule: BandDelightSchedule | None = None,
    transient_overlay: bool = False,
) -> dict[str, object]:
    row = repo.get_band_row_for_chapter(project_id, chapter_number)
    live_schedule = schedule or repo.get_band_experience_plan_for_chapter(project_id, chapter_number)
    if row is None and live_schedule is None:
        return {}
    return {
        "band_id": str(getattr(row, "band_id", getattr(live_schedule, "band_id", "")) or ""),
        "chapter_start": int(getattr(row, "chapter_start", getattr(live_schedule, "chapter_start", 0)) or 0),
        "chapter_end": int(getattr(row, "chapter_end", getattr(live_schedule, "chapter_end", 0)) or 0),
        "task_contract": self._load_json_list(getattr(row, "task_contract_json", "[]")),
        "schedule": live_schedule.model_dump(mode="json") if live_schedule is not None else {},
        "transient_overlay": bool(transient_overlay),
    }

@staticmethod
def _repair_verification_issue(
    *,
    rule_name: str,
    description: str,
    suggested_fix: str,
) -> ContinuityIssue:
    return ContinuityIssue(
        rule_name=rule_name,
        severity="error",
        description=description,
        reviewer="repair_verifier",
        issue_type="repair_verification",
        target_scope="chapter",
        evidence_refs=[],
        suggested_fix=suggested_fix,
    )

def _review_with_repair_verification(
    self,
    *,
    original_output: WriterOutput,
    repaired_output: WriterOutput,
    before_review: ReviewVerdict,
    review: ReviewVerdict,
    repair_instruction: RepairInstruction,
) -> ReviewVerdict:
    verification = self.repair_verifier.verify(
        original_output=original_output,
        repaired_output=repaired_output,
        before_review=before_review,
        after_review=review,
        repair_instruction=repair_instruction,
    )
    merged_review = review.model_copy(update={"repair_verification": verification})
    if verification.fixed_all_must_fix and verification.preserved_all_must_preserve:
        return merged_review

    issues = list(merged_review.issues)
    for item in verification.unfixed:
        issues.append(
            self._repair_verification_issue(
                rule_name="repair_unfixed",
                description=f"repair 未真正修复：{item}",
                suggested_fix="升级 repair scope，并继续针对 must_fix 重写。",
            )
        )
    for item in verification.broken_preserve_constraints:
        issues.append(
            self._repair_verification_issue(
                rule_name="repair_preserve_breach",
                description=f"repair 破坏了 must_preserve：{item}",
                suggested_fix="保留既有约束后重新修复，不允许以修 A 伤 B。",
            )
        )
    summary_parts = [str(merged_review.review_summary or "").strip()]
    if verification.unfixed:
        summary_parts.append("repair verification: must_fix 仍未完全修复")
    if verification.broken_preserve_constraints:
        summary_parts.append("repair verification: must_preserve 被破坏")
    return merged_review.model_copy(
        update={
            "verdict": "fail",
            "recommended_action": "rewrite",
            "issues": issues,
            "review_summary": " | ".join(part for part in summary_parts if part),
            "repair_instruction": merged_review.repair_instruction or repair_instruction,
        }
    )

@staticmethod
def _repair_policy_requested_scope(review: ReviewVerdict) -> str:
    instruction = getattr(review, "repair_instruction", None)
    if instruction is None:
        return ""
    requested_scope = str(getattr(instruction, "repair_scope", "") or "").strip()
    if WritingOrchestrator._review_has_structural_repair_issue(review):
        return ""
    return requested_scope

@staticmethod
def _review_has_structural_repair_issue(review: ReviewVerdict) -> bool:
    structural_issue_types = {
        "countdown_non_monotonic",
        "artifact_count_explanation",
        "artifact_ledger_conflict",
        "identity_conflict",
        "identity_ambiguity",
        "payoff_miss",
        "unpaid_promise_debt",
        "world_model_conflict",
        "cognition_conflict",
    }
    structural_target_scopes = {
        "ledger",
        "character",
        "band",
        "arc",
        "book",
        "world_model",
    }
    for issue in getattr(review, "issues", []) or []:
        issue_type = str(getattr(issue, "issue_type", "") or "").strip()
        target_scope = str(getattr(issue, "target_scope", "") or "").strip()
        if issue_type in structural_issue_types or target_scope in structural_target_scopes:
            return True
    return False



__all__ = ['_persist_draft_and_review', '_review_current_output', '_apply_canon_name_drift_autofix', '_apply_subworld_admission_autofix', '_apply_placeholder_leakage_autofix', '_placeholder_role_replacement', '_looks_like_genericizable_unknown_reference', '_project_character_names', '_generic_subworld_reference', '_subworld_role_titles', '_replace_canon_name_strings', '_review_event_payload', '_review_issue_payloads', '_record_map_movement_review_issues', '_review_canon_risk', '_load_json_list', '_chapter_plan_snapshot', '_band_plan_snapshot', '_repair_verification_issue', '_review_with_repair_verification', '_repair_policy_requested_scope', '_review_has_structural_repair_issue']
