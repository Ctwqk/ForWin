from __future__ import annotations

import json

from forwin.canon_quality.signals import CanonQualitySignal


class FakeJsonClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages: list[dict] = []

    def chat(self, messages: list[dict], **kwargs):  # noqa: ANN001
        self.messages = messages
        return json.dumps(self.payload, ensure_ascii=False)


def _issue(
    *,
    severity: str = "critical",
    blocking: bool = True,
    confidence: float = 0.92,
    evidence: list[dict] | None = None,
    issue_type: str = "identity_contradiction",
) -> dict:
    return {
        "issue_id": "issue-1",
        "type": issue_type,
        "severity": severity,
        "blocking": blocking,
        "confidence": confidence,
        "claim": "正文直接改写了已锁定身份。",
        "evidence": evidence if evidence is not None else [
            {"source": "writer_output", "quote": "她就是另一个人。", "location": "body:10-18"}
        ],
        "reasoning_summary": "直接证据足够。",
        "suggested_fix": "保留既有身份或补充明确伪装桥接。",
    }


def _result(**overrides: object) -> dict:
    payload = {
        "analyzer": "IdentityConsistencyPromptAnalyzer",
        "version": "1.0",
        "verdict": "fail",
        "blocking": True,
        "confidence": 0.92,
        "summary": "发现一项身份冲突。",
        "issues": [_issue()],
        "evidence": [],
        "uncertainties": [],
        "accepted_facts": [],
        "metadata": {"prompt_version": "1.0"},
    }
    payload.update(overrides)
    return payload


def test_prompt_json_blocking_policy_requires_critical_confident_evidence() -> None:
    from forwin.canon_quality.prompt_json.validation import result_can_block

    assert result_can_block(_result()) is True
    assert result_can_block(_result(verdict="warn")) is False
    assert result_can_block(_result(verdict="uncertain")) is False
    assert result_can_block(_result(issues=[_issue(evidence=[])])) is False
    assert result_can_block(_result(issues=[_issue(confidence=0.79)])) is False
    assert result_can_block(_result(issues=[_issue(severity="major")])) is False


def test_legacy_error_hint_does_not_block_without_prompt_evidence() -> None:
    from forwin.canon_quality.prompt_json.normalization import legacy_signals_to_prompt_result
    from forwin.canon_quality.prompt_json.validation import result_can_block

    legacy_signal = CanonQualitySignal(
        signal_id="legacy-countdown",
        project_id="p1",
        chapter_number=3,
        signal_type="countdown_non_monotonic",
        severity="error",
        target_scope="ledger",
        subject_key="countdown:main",
        description="旧规则认为倒计时回升。",
        evidence_refs=["body:1-8"],
    )

    result = legacy_signals_to_prompt_result(
        analyzer="CountdownLedgerPromptAnalyzer",
        legacy_signals=[legacy_signal],
        mode="hybrid",
    )

    assert result["verdict"] == "uncertain"
    assert result["blocking"] is False
    assert result_can_block(result) is False
    assert result["metadata"]["legacy_signal_count"] == 1


def test_prompt_analyzer_normalizes_strict_json_and_preserves_metadata() -> None:
    from forwin.canon_quality.prompt_json.identity_prompt import IdentityConsistencyPromptAnalyzer

    client = FakeJsonClient(
        {
            "verdict": "pass",
            "blocking": False,
            "confidence": 0.91,
            "summary": "少主、李公子和那人都由上下文支持为同一角色称谓。",
            "identity_mentions": [
                {
                    "surface_name": "少主",
                    "resolved_character_id": "li",
                    "resolution_confidence": 0.9,
                    "identity_mode": "alias",
                    "speaker_or_pov": "旁白",
                    "is_supported_by_context": True,
                    "evidence_quote": "少主李公子抬头。",
                }
            ],
            "issues": [],
            "uncertainties": [],
        }
    )

    result = IdentityConsistencyPromptAnalyzer(llm_client=client).analyze(
        {
            "writer_output": "少主李公子抬头，那人没有暴露真名。",
            "identity_registry": [
                {
                    "character_id": "li",
                    "canonical_name": "李公子",
                    "aliases": ["少主"],
                    "disguises": [],
                    "known_false_identities": [],
                    "identity_constraints": [],
                }
            ],
            "scene_context": {"pov": "旁白", "who_knows_what": []},
            "canon_context": [],
            "heuristic_hints": [],
        }
    )

    assert result["analyzer"] == "IdentityConsistencyPromptAnalyzer"
    assert result["version"] == "1.0"
    assert result["verdict"] == "pass"
    assert result["blocking"] is False
    assert result["metadata"]["prompt_version"] == "1.0"
    assert "heuristic_hints are suggestions only" in client.messages[0]["content"]


def test_reviewer_prompt_json_issues_preserve_source_metadata() -> None:
    from forwin.reviewer.hub import HistoricalReviewHub

    issues = HistoricalReviewHub._canon_quality_issues(
        {
            "prompt_json_results": [
                _result(
                    analyzer="CountdownLedgerPromptAnalyzer",
                    verdict="fail",
                    confidence=0.88,
                    issues=[
                        _issue(
                            issue_type="countdown_contradiction",
                            evidence=[
                                {"source": "writer_output", "quote": "倒计时从 10 分钟回到 30 分钟。", "location": "body:1-20"}
                            ],
                        )
                    ],
                )
            ],
            "blocking_signals": [],
            "warning_signals": [],
        }
    )

    assert len(issues) == 1
    issue = issues[0]
    assert issue.reviewer == "canon_quality"
    assert issue.issue_type == "countdown_contradiction"
    assert issue.source_layer == "canon_quality"
    assert issue.source_analyzer == "CountdownLedgerPromptAnalyzer"
    assert issue.source_mode == "prompt_json"
    assert issue.original_verdict == "fail"
    assert issue.original_confidence == 0.88
    assert issue.blocking_origin == "prompt_json"
    assert issue.evidence_refs == ["writer_output:body:1-20"]


def test_final_prompt_gate_admits_plan_update_instead_of_rejecting() -> None:
    from forwin.gate.prompt_json.final_canon_gate_prompt import evaluate_final_canon_gate

    decision = evaluate_final_canon_gate(
        {
            "writer_output": "新角色留下了一条可用线索。",
            "analyzer_results": [
                _result(
                    analyzer="FuturePlanPromptAuditor",
                    verdict="warn",
                    blocking=False,
                    confidence=0.86,
                    issues=[
                        _issue(
                            severity="major",
                            blocking=False,
                            issue_type="plan_needs_update",
                            evidence=[
                                {"source": "writer_output", "quote": "新角色留下了一条可用线索。", "location": "body:0-12"}
                            ],
                        )
                    ],
                )
            ],
            "canon_admission_policy": {
                "min_blocking_confidence": 0.8,
                "require_evidence_for_block": True,
            },
        }
    )

    assert decision["decision"] == "admit_with_plan_update"
    assert decision["blocking"] is False
    assert decision["required_patches"][0]["patch_type"] == "plan_patch"


def test_canon_admission_gate_consumes_raw_prompt_json_blockers() -> None:
    from forwin.canon_quality.gate import evaluate_canon_admission

    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=3,
        draft_id="d1",
        review_id="r1",
        review_verdict="pass",
        signals=[],
        analyzer_results=[_result()],
        mode="strict",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.llm_issue_refs == ["IdentityConsistencyPromptAnalyzer:issue-1"]


def test_future_plan_auditor_prompt_json_warn_creates_nonblocking_plan_patch() -> None:
    from forwin.models import ChapterPlan
    from forwin.planning.future_plan_auditor import FuturePlanAuditor

    client = FakeJsonClient({
        "analyzer": "FuturePlanPromptAuditor",
        "version": "1.0",
        "verdict": "warn",
        "blocking": False,
        "confidence": 0.86,
        "summary": "新线索需要更新未来计划，但不构成 canon contradiction。",
        "plan_impacts": [
            {
                "plan_item_id": "plan-4",
                "plan_item_description": "第四章继续原计划",
                "lock_level": "soft",
                "impact_type": "needs_update",
                "evidence_quote": "新角色留下了一条可用线索。",
                "recommended_plan_patch": "把新线索纳入第四章目标。",
                "confidence": 0.86,
            }
        ],
        "issues": [
            _issue(
                severity="major",
                blocking=False,
                issue_type="plan_needs_update",
                evidence=[
                    {"source": "writer_output", "quote": "新角色留下了一条可用线索。", "location": "body:0-12"}
                ],
            )
        ],
        "uncertainties": [],
    })
    plan = ChapterPlan(
        id="plan-4",
        project_id="p1",
        arc_plan_id="arc-1",
        chapter_number=4,
        title="第四章",
        one_line="继续原计划",
        goals_json="[]",
        task_contract_json="[]",
        experience_plan_json="{}",
        status="planned",
    )

    result = FuturePlanAuditor(mode="prompt_json", llm_client=client).audit_plans(
        project_id="p1",
        current_chapter=3,
        trigger_stage="post_acceptance",
        plans=[plan],
        canon_quality_context={"writer_output": "新角色留下了一条可用线索。"},
        obligations=[],
        target_total_chapters=10,
        include_current=False,
    )

    assert result.status == "warn"
    assert result.issues[0].issue_type == "plan_needs_update"
    assert result.issues[0].blocking is False
    assert result.issues[0].metadata["source_analyzer"] == "FuturePlanPromptAuditor"
    assert result.plan_patches[0].patch_type == "future_plan_prompt_update"
    assert result.plan_patches[0].target_plan_id == "plan-4"


def test_plan_patch_validator_prompt_json_blocks_evidence_backed_critical_issue() -> None:
    from forwin.narrative_obligations.types import NarrativePlanPatch
    from forwin.planning.plan_patch_validator import PlanPatchValidator

    patch = NarrativePlanPatch(
        id="patch-1",
        project_id="p1",
        target_scope="chapter",
        affected_chapters=[4],
        writer_context_injections=[{"instruction": "update"}],
        reviewer_context_injections=[{"instruction": "check"}],
        expected_resolution_tests=["keeps locked fact"],
    )
    client = FakeJsonClient({
        "analyzer": "PlanPatchPromptValidator",
        "version": "1.0",
        "verdict": "fail",
        "blocking": True,
        "confidence": 0.91,
        "summary": "patch 删除了锁定约束。",
        "patch_assessment": {
            "allowed_fields_only": False,
            "canon_preserved": False,
            "locked_constraints_preserved": False,
            "patch_addresses_claimed_issue": False,
            "requires_human_review": True,
        },
        "field_changes": [],
        "issues": [
            _issue(
                issue_type="locked_constraint_removed",
                evidence=[
                    {"source": "proposed_patch", "quote": "remove locked constraint", "location": "patch:/constraints/0"}
                ],
            )
        ],
        "uncertainties": [],
    })

    result = PlanPatchValidator(mode="prompt_json", llm_client=client).validate(
        patch=patch,
        obligations=[],
        current_chapter=3,
        target_total_chapters=10,
    )

    assert result.passed is False
    assert result.errors == ["prompt_json:locked_constraint_removed"]


def test_obligation_identity_and_cliffhanger_prompt_analyzers_keep_non_blocking_cases() -> None:
    from forwin.canon_quality.prompt_json.final_completion_prompt import FinalCompletionPromptAnalyzer
    from forwin.canon_quality.prompt_json.identity_prompt import IdentityConsistencyPromptAnalyzer
    from forwin.canon_quality.prompt_json.obligation_verifier_prompt import ObligationVerifierPromptAnalyzer
    from forwin.canon_quality.prompt_json.validation import result_can_block

    obligation = ObligationVerifierPromptAnalyzer(llm_client=FakeJsonClient({
        "verdict": "pass",
        "blocking": False,
        "confidence": 0.84,
        "summary": "未出现的 open obligation 当前未到期，不阻断。",
        "obligation_status": [],
        "issues": [],
        "uncertainties": [],
    })).analyze({
        "writer_output": "本章处理另一条支线。",
        "obligation_ledger": [
            {
                "obligation_id": "open-1",
                "description": "以后兑现承诺",
                "holder": "主角",
                "target": "伙伴",
                "status": "open",
                "must_address_in_current_output": False,
                "failure_condition": "",
                "source": "plan",
            }
        ],
        "chapter_plan": "推进支线",
        "canon_context": [],
        "heuristic_hints": [],
    })
    identity = IdentityConsistencyPromptAnalyzer(llm_client=FakeJsonClient({
        "verdict": "pass",
        "blocking": False,
        "confidence": 0.9,
        "summary": "别名使用有上下文支持。",
        "identity_mentions": [],
        "knowledge_violations": [],
        "issues": [],
        "uncertainties": [],
    })).analyze({
        "writer_output": "少主、李公子和那人指向同一人。",
        "identity_registry": [],
        "scene_context": {"pov": "旁白", "who_knows_what": []},
        "canon_context": [],
        "heuristic_hints": [],
    })
    completion = FinalCompletionPromptAnalyzer(llm_client=FakeJsonClient({
        "verdict": "warn",
        "blocking": False,
        "confidence": 0.82,
        "summary": "计划中的 cliffhanger 有效。",
        "completion_assessment": {
            "ending_type_detected": "cliffhanger",
            "matches_planned_ending_type": True,
            "chapter_goal_satisfied": True,
            "scene_goal_satisfaction": [],
        },
        "required_beat_status": [],
        "issues": [],
        "uncertainties": [],
    })).analyze({
        "writer_output": "门后传来新的敲击声。",
        "chapter_goal": "找到门",
        "scene_goals": ["制造悬念"],
        "required_beats": [],
        "open_threads": [],
        "planned_ending_type": "cliffhanger",
        "heuristic_hints": [],
    })

    assert result_can_block(obligation) is False
    assert result_can_block(identity) is False
    assert result_can_block(completion) is False
