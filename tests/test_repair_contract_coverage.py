from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.api_schema.review import RepairVerificationInfo
from forwin.application.projects.reviews import _build_decision_layers
from forwin.canon.eligibility import candidate_ineligibility_reason
from forwin.protocol.review import (
    ContinuityIssue,
    RepairInstruction,
    RepairVerification,
    ReviewVerdict,
)
from forwin.protocol.writer import WriterOutput
from forwin.review.decision.rules.final_residual import FinalResidualPolicy
from forwin.review.repair.verification import RepairVerifier
from forwin.review.results import merge_repair_verification


class ScriptedVerifier:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response)


def verify(
    client=None,
    *,
    original="她答应留下。",
    final="她抛下同伴离去。",
    preserve=None,
    fixes=None,
    secrets=None,
):
    return RepairVerifier(llm_client=client, llm_enabled=client is not None).verify(
        original_output=WriterOutput(
            chapter_number=1, title="誓约", body=original, end_of_chapter_summary=""
        ),
        repaired_output=WriterOutput(
            chapter_number=1, title="誓约", body=final, end_of_chapter_summary=""
        ),
        before_review=ReviewVerdict(verdict="fail"),
        after_review=ReviewVerdict(verdict="pass"),
        repair_instruction=RepairInstruction(
            repair_scope="draft",
            failure_type="mixed",
            must_preserve=preserve or [],
            must_fix=fixes or [],
            must_not_reveal=secrets or [],
        ),
    )


def evidence(source, quote, start=0):
    return {"source": source, "quote": quote, "start": start, "end": start + len(quote)}


def check(contract_id, status, *refs):
    return {
        "contract_id": contract_id,
        "status": status,
        "reason": "正文表明约定的行为发生变化。",
        "evidence": list(refs),
    }


def test_rule_title_pass_does_not_claim_semantic_preservation():
    result = verify(preserve=["誓约", "同伴相互支持"])
    assert result.preserved_all_must_preserve is None
    assert [(item.contract_id, item.status) for item in result.checks] == [
        ("must_preserve:1", "pass"),
        ("must_preserve:2", "unknown"),
    ]
    assert result.broken_preserve_constraints == []


def test_middle_reveal_is_visible_and_confirmed_with_one_same_verifier_recheck():
    original = "甲" * 1500 + "秘密无人知道。" + "乙" * 1500
    final = "甲" * 1500 + "王后就是叛徒。" + "乙" * 1500
    response = {
        "fixed_all_must_fix": True,
        "preserved_all_must_preserve": True,
        "checks": [
            check(
                "must_not_reveal:1",
                "fail",
                evidence("repaired_body", "王后就是叛徒。", 1500),
            )
        ],
    }
    client = ScriptedVerifier(response, response)
    result = verify(
        client,
        original=original,
        final=final,
        preserve=["誓约"],
        secrets=["不得揭露王后是叛徒"],
    )
    assert result.preserved_all_must_preserve is False
    assert result.broken_preserve_constraints == ["不得揭露王后是叛徒"]
    assert result.recheck_count == 1
    assert len(client.requests) == 2
    for messages, kwargs in client.requests:
        payload = json.loads(messages[1]["content"])
        assert payload["original_draft"]["body"] == original
        assert payload["repaired_draft"]["body"] == final
        assert kwargs["timeout_seconds"] == 30
        assert kwargs["retry_on_timeout"] is False


def test_semantic_preserve_breach_survives_identical_title():
    response = {
        "checks": [
            check(
                "must_preserve:2",
                "fail",
                evidence("original_body", "她答应留下。"),
                evidence("repaired_body", "她抛下同伴离去。"),
            )
        ]
    }
    result = verify(
        ScriptedVerifier(response, response),
        preserve=["誓约", "保留留下陪伴同伴的承诺"],
    )
    assert result.preserved_all_must_preserve is False
    assert [item.status for item in result.checks] == ["pass", "fail"]


@pytest.mark.parametrize(
    "ref",
    [
        evidence("repaired_body", "伪造引用"),
        evidence("repaired_body", "她抛下同伴离去。", 1),
        evidence("original_body", "她抛下同伴离去。"),
    ],
)
def test_invalid_quote_offset_or_source_is_unknown_without_recheck(ref):
    client = ScriptedVerifier({"checks": [check("must_not_reveal:1", "fail", ref)]})
    result = verify(client, secrets=["保守秘密"])
    assert result.preserved_all_must_preserve is None
    assert result.checks[0].status == "unknown"
    assert result.checks[0].evidence == []
    assert len(client.requests) == 1


def test_llm_top_level_booleans_are_not_contract_evidence():
    client = ScriptedVerifier(
        {
            "fixed_all_must_fix": False,
            "preserved_all_must_preserve": False,
            "unfixed": ["无证据反对"],
        }
    )
    result = verify(client, fixes=["维持人物动机"], preserve=["誓约"])
    assert result.fixed_all_must_fix is None
    assert result.preserved_all_must_preserve is True
    assert result.unfixed == []
    assert len(client.requests) == 1


def test_all_contracts_are_sent_and_omitted_ninth_result_stays_unknown():
    constraints = [f"保留线索{i}" for i in range(1, 10)]
    response = {
        "checks": [
            check(
                f"must_preserve:{i}",
                "pass",
                evidence("original_body", "她答应留下。"),
                evidence("repaired_body", "她抛下同伴离去。"),
            )
            for i in range(1, 9)
        ]
    }
    client = ScriptedVerifier(response)
    result = verify(client, preserve=constraints)
    payload = json.loads(client.requests[0][0][1]["content"])
    assert [item["constraint"] for item in payload["contracts"]] == constraints
    assert len(result.checks) == 9
    assert result.checks[-1].status == "unknown"
    assert result.preserved_all_must_preserve is None


def test_complete_valid_semantic_evidence_earns_pass():
    response = {
        "fixed_all_must_fix": False,
        "checks": [
            check(
                "must_fix:1",
                "pass",
                evidence("original_body", "旧错误"),
                evidence("repaired_body", "已纠正"),
            )
        ],
    }
    result = verify(
        ScriptedVerifier(response),
        original="旧错误",
        final="已纠正",
        fixes=["纠正错误"],
    )
    assert result.fixed_all_must_fix is True
    assert result.checks[0].method == "llm"


@pytest.mark.parametrize("response", [TimeoutError("offline"), {"checks": "invalid"}])
def test_timeout_or_invalid_output_stays_unknown_without_retry(response):
    client = ScriptedVerifier(response)
    result = verify(client, fixes=["纠正动机"])
    assert result.fixed_all_must_fix is None
    assert result.recheck_count == 0
    assert len(client.requests) == 1


def test_recheck_timeout_leaves_evidence_backed_opposition_unknown():
    response = {
        "checks": [
            check(
                "must_not_reveal:1",
                "fail",
                evidence("repaired_body", "她抛下同伴离去。"),
            )
        ]
    }
    client = ScriptedVerifier(response, TimeoutError("offline"))
    result = verify(client, secrets=["隐藏离去的安排"])
    assert result.preserved_all_must_preserve is None
    assert result.recheck_count == 1
    assert result.broken_preserve_constraints == []


def test_oversize_body_marks_unknown_without_sending_cropped_json():
    client = ScriptedVerifier({})
    result = verify(client, original="原" * 25000, fixes=["修复中段"])
    assert result.fixed_all_must_fix is None
    assert result.checks[0].reason == "input_budget_exceeded"
    assert client.requests == []


def test_unknown_roundtrips_api_and_does_not_create_repair_or_canon_failure():
    result = RepairVerification.model_validate(
        {"fixed_all_must_fix": None, "preserved_all_must_preserve": None}
    )
    assert (
        RepairVerificationInfo.model_validate(result.model_dump()).model_dump()[
            "fixed_all_must_fix"
        ]
        is None
    )
    assert RepairVerification().fixed_all_must_fix is False
    assert RepairVerificationInfo().fixed_all_must_fix is False
    review = ReviewVerdict(verdict="pass", repair_verification=result)
    assert candidate_ineligibility_reason(review) == ""
    merged = merge_repair_verification(review, result, None)
    assert merged.verdict == "pass"
    assert merged.issues == []
    layers = _build_decision_layers(
        review=review,
        review_meta={},
        issues=[],
        rewrite_attempts=[
            SimpleNamespace(
                result_verdict="pass", verification_json=result.model_dump_json()
            )
        ],
        candidate=None,
        decision_refs=[],
    )
    assert next(item for item in layers if item.layer == "repair").blocking is False


def test_unknown_does_not_weaken_primary_review_hard_residual_or_known_failure():
    unknown = RepairVerification.model_construct(
        fixed_all_must_fix=None, preserved_all_must_preserve=None
    )
    hard = ContinuityIssue(
        rule_name="continuity",
        issue_type="continuity",
        description="冲突",
        severity="error",
    )
    assert candidate_ineligibility_reason(
        ReviewVerdict(verdict="fail", repair_verification=unknown)
    )
    assert candidate_ineligibility_reason(
        ReviewVerdict(
            verdict="pass", repair_verification=unknown, residual_review_issues=[hard]
        )
    )
    assert candidate_ineligibility_reason(
        ReviewVerdict(verdict="pass", repair_verification=RepairVerification())
    )
    policy = FinalResidualPolicy()
    assert (
        policy.evaluate(
            review=ReviewVerdict(verdict="fail", issues=[hard]), verification=unknown
        ).forceable
        is False
    )
    soft = hard.model_copy(update={"issue_type": "stall"})
    assert (
        policy.evaluate(
            review=ReviewVerdict(verdict="fail", issues=[soft]), verification=unknown
        ).forceable
        is True
    )


@pytest.mark.parametrize("changed", [False, True])
def test_deterministic_title_result_wins_over_opposing_llm(changed):
    client = ScriptedVerifier(
        {
            "checks": [
                check(
                    "must_preserve:1",
                    "pass" if changed else "fail",
                    evidence("original_body", "原稿"),
                    evidence("repaired_body", "修复稿"),
                ),
                check("must_fix:1", "unknown"),
            ]
        }
    )
    result = RepairVerifier(llm_client=client, llm_enabled=True).verify(
        original_output=WriterOutput(
            chapter_number=1, title="誓约", body="原稿", end_of_chapter_summary=""
        ),
        repaired_output=WriterOutput(
            chapter_number=1,
            title="终局" if changed else "誓约",
            body="修复稿",
            end_of_chapter_summary="",
        ),
        before_review=ReviewVerdict(verdict="fail"),
        after_review=ReviewVerdict(verdict="pass"),
        repair_instruction=RepairInstruction(
            repair_scope="draft",
            failure_type="mixed",
            must_preserve=["誓约"],
            must_fix=["改善动机"],
        ),
    )
    assert result.preserved_all_must_preserve is (not changed)
    assert result.recheck_count == 0
    assert result.checks[1].method == "exact_title"


def test_conflicting_recheck_and_duplicate_contract_results_stay_unknown():
    refs = [
        evidence("original_body", "她答应留下。"),
        evidence("repaired_body", "她抛下同伴离去。"),
    ]
    client = ScriptedVerifier(
        {"checks": [check("must_preserve:1", "fail", *refs)]},
        {"checks": [check("must_preserve:1", "pass", *refs)]},
    )
    assert verify(client, preserve=["保持承诺"]).preserved_all_must_preserve is None
    duplicate = check("must_preserve:1", "pass", *refs)
    result = verify(
        ScriptedVerifier({"checks": [duplicate, duplicate]}), preserve=["保持承诺"]
    )
    assert result.preserved_all_must_preserve is None


def test_oversize_contract_is_unknown_without_partial_json():
    client = ScriptedVerifier({})
    result = verify(client, fixes=["约束" * 25000])
    assert result.fixed_all_must_fix is None
    assert result.checks[0].constraint == "约束" * 25000
    assert result.checks[0].reason == "input_budget_exceeded"
    assert client.requests == []


def test_contract_results_survive_api_serialization():
    result = verify(preserve=["誓约", "保留人物动机"])
    api = RepairVerificationInfo.model_validate_json(result.model_dump_json())
    assert api.checks[0].evidence[0].quote == "誓约"
    assert api.checks[1].status == "unknown"
    assert api.preserved_all_must_preserve is None


def test_review_modal_displays_unknown_and_each_contract(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    script = Path("forwin/ui_assets/home/app_task_progress.js").read_text()
    fixture = r"""
const elements = new Map();
function createNode(tag, text = '', cls = '') {
  return { textContent: text, children: [], dataset: {}, appendChild(child) { this.children.push(child); }, append(...children) { this.children.push(...children); } };
}
function createButton(text) { return createNode('button', text); }
function clearNode(node) { node.children = []; }
const document = { getElementById(id) { if (!elements.has(id)) elements.set(id, createNode('div')); return elements.get(id); } };
renderReviewModal({ rewrite_attempts: [{attempt_no: 1, result_verdict: 'pass', verification: {fixed_all_must_fix: null, preserved_all_must_preserve: true, checks: [{contract_id: 'must_fix:1', constraint: '保留人物动机', status: 'unknown', reason: '缺少证据', method: 'unverified', evidence: []}]}}]}, 'p', 1);
function allText(node) { return [node.textContent, ...node.children.map(allText)].join(' '); }
console.log(allText(elements.get('review_repair_list')));
"""
    path = tmp_path / "repair-ui.js"
    path.write_text(script + fixture)
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, check=True
    )
    assert "must-fix 未验证" in result.stdout
    assert "must-preserve" in result.stdout and "通过" in result.stdout
    assert "保留人物动机" in result.stdout
    assert "缺少证据" in result.stdout
    assert "未通过" not in result.stdout


def test_truncated_verifier_json_is_not_repaired_into_evidence():
    response = {
        "checks": [
            check(
                "must_fix:1",
                "pass",
                evidence("original_body", "旧错误"),
                evidence("repaired_body", "已纠正"),
            )
        ]
    }
    client = ScriptedVerifier(json.dumps(response)[:-1])
    result = verify(client, original="旧错误", final="已纠正", fixes=["纠正错误"])
    assert result.fixed_all_must_fix is None
    assert result.checks[0].status == "unknown"
    assert len(client.requests) == 1


def test_diagnostic_bulk_does_not_displace_complete_body_or_contracts():
    bulk = "无关诊断" * 25000
    response = {
        "checks": [
            check(
                "must_fix:1",
                "pass",
                evidence("original_body", "旧错误"),
                evidence("repaired_body", "已纠正"),
            )
        ]
    }
    client = ScriptedVerifier(response)
    result = RepairVerifier(llm_client=client, llm_enabled=True).verify(
        original_output=WriterOutput(
            chapter_number=1,
            title="誓约",
            body="旧错误",
            end_of_chapter_summary="",
            generation_meta={"raw_prompt": bulk},
        ),
        repaired_output=WriterOutput(
            chapter_number=1, title="誓约", body="已纠正", end_of_chapter_summary=""
        ),
        before_review=ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="motivation",
                    description="动机错误",
                    evidence_refs=[bulk],
                    suggested_fix=bulk,
                )
            ],
            prompt_trace={"raw_prompt": bulk},
        ),
        after_review=ReviewVerdict(verdict="pass"),
        repair_instruction=RepairInstruction(
            repair_scope="draft",
            failure_type="mixed",
            must_fix=["纠正错误"],
            evidence_refs=[bulk],
            design_patch={"raw_context": bulk},
        ),
    )
    assert result.fixed_all_must_fix is True
    content = client.requests[0][0][1]["content"]
    assert content.count("纠正错误") == 1
    assert bulk not in content
    payload = json.loads(content)
    assert payload["original_draft"]["body"] == "旧错误"
    assert payload["repaired_draft"]["body"] == "已纠正"
