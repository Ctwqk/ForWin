from __future__ import annotations

import copy
import hashlib
import json

import pytest

from forwin.protocol.writer import WriterOutput

DIMENSIONS = ("possession", "knowledge", "life_state", "time", "place", "obligations")
BODY = "林青取出铜钥匙，打开北门。他还不知道城主已经死去。"


def _prefix():
    return {
        "project_id": "book",
        "through_chapter": 3,
        "complete": True,
        "facts": {
            "possession": [{"subject": "林青", "owns": "铜钥匙"}],
            "knowledge": [{"subject": "林青", "knows": []}],
            "life_state": [{"subject": "城主", "state": "dead"}],
            "time": {"day": 2},
            "place": {"location": "北门"},
            "obligations": [],
        },
        "character_rows": [],
        "countdown_rows": [],
        "open_signal_rows": [],
        "obligations": [],
    }


class FormClient:
    """Replace only the external model; form building/validation/projectors stay real."""

    def __init__(self, mutate=None):
        self.mutate = mutate
        self.requests = []
        self.llm_attempt_events = []

    def complete_json(self, *, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        self.requests.append(payload)
        form = payload["form"]
        answer = {
            "project_id": form["project_id"],
            "chapter_number": form["chapter_number"],
            "form_schema_version": form["form_schema_version"],
            "chapter_summary": "林青开门。",
            "characters": [
                {
                    "name": item["name"],
                    "appears_in_chapter": False,
                    "life_state": {"value": "unknown"},
                    "custody_state": {"value": "unknown"},
                    "participation": {"value": "unknown"},
                }
                for item in form["characters"]
            ],
            "countdowns": [
                {
                    "key": item["key"],
                    "mentioned_in_chapter": False,
                    "status_in_this_chapter": {"value": "unknown"},
                    "consistent_with_prior": {"value": "unknown"},
                }
                for item in form["countdowns"]
            ],
            "obligations": [
                {"id": item["id"], "addressed": {"value": "unknown"}}
                for item in form["obligations"]
            ],
            "open_signals": [
                {"id": item["id"], "status": {"value": "unknown", "confidence": 0}}
                for item in form["open_signals"]
            ],
        }
        result = {
            "answers": answer,
            "body_sha256": payload["body_sha256"],
            "prefix_sha256": payload["prefix_sha256"],
            "coverage_complete": True,
            "coverage": [
                {
                    "dimension": dimension,
                    "status": "pass",
                    "explanation": "已按完整正文逐项核对修改后的前缀，本章没有与此前状态冲突的变化。",
                    "body_evidence": [{"start": 0, "end": len(BODY), "quote": BODY}],
                    "prefix_refs": [f"/facts/{dimension}"],
                }
                for dimension in DIMENSIONS
            ],
        }
        if self.mutate:
            return self.mutate(result)
        return result


def _review(client=None, prefix=None, generation_meta=None, **kwargs):
    from forwin.canon_quality.chapter_review_form.historical import (
        review_historical_chapter_with_form,
    )

    return review_historical_chapter_with_form(
        session=None,
        project_id="book",
        chapter_number=4,
        writer_output=WriterOutput(
            project_id="book",
            chapter_number=4,
            title="北门",
            body=BODY,
            end_of_chapter_summary="旧摘要不能充当正文证据。",
            generation_meta=generation_meta or {},
        ),
        llm_client=client,
        prefix_context=_prefix() if prefix is None else prefix,
        **kwargs,
    )


def _statuses(result):
    return {check.dimension: check.status for check in result.checks}


def test_historical_review_returns_grounded_six_dimension_checks_in_one_form_call():
    client = FormClient()
    result = _review(client)
    assert _statuses(result) == {dimension: "pass" for dimension in DIMENSIONS}
    body_hash = hashlib.sha256(BODY.encode()).hexdigest()
    for check in result.checks:
        assert f"body:{body_hash}#0:{len(BODY)}" in check.evidence_refs
        assert check.explanation
    assert result.review.answers.chapter_summary == "林青开门。"
    assert result.review.blocking is False
    assert len(client.requests) == 1  # Six checks extend the same semantic review.
    assert client.requests[0]["chapter_body"] == BODY
    assert client.requests[0]["prefix_context"] == _prefix()
    assert "旧摘要" not in json.dumps(client.requests, ensure_ascii=False)


def test_missing_dimension_is_unknown_instead_of_inferred_from_other_passes():
    client = FormClient(lambda result: {**result, "coverage": result["coverage"][:-1]})
    result = _review(client)
    assert _statuses(result)["obligations"] == "unknown"
    assert _statuses(result)["possession"] == "pass"
    assert result.review.blocking


@pytest.mark.parametrize(
    "fault", ["quote", "range", "prefix", "duplicate", "explanation"]
)
def test_invalid_dimension_evidence_cannot_be_promoted_to_pass(fault):
    def mutate(result):
        check = result["coverage"][0]
        if fault == "quote":
            check["body_evidence"][0]["quote"] = "旧元数据说林青有钥匙"
        elif fault == "range":
            check["body_evidence"][0]["start"] = -1
        elif fault == "prefix":
            check["prefix_refs"] = ["/facts/possession/nonexistent"]
        elif fault == "duplicate":
            result["coverage"].append(copy.deepcopy(check))
        else:
            check["explanation"] = ""
        return result

    result = _review(FormClient(mutate))
    assert _statuses(result)["possession"] == "unknown"
    assert result.review.blocking


def test_exact_body_grounded_failure_remains_fail():
    def mutate(result):
        result["coverage"][0].update(
            status="fail", explanation="前缀已失去钥匙，本章却仍用它开门。"
        )
        return result

    result = _review(FormClient(mutate))
    assert _statuses(result)["possession"] == "fail"
    assert result.review.blocking


@pytest.mark.parametrize(
    "fault",
    [
        "body_hash",
        "prefix_hash",
        "identity",
        "complete",
        "truncated_json",
        "truncated_flag",
    ],
)
def test_incomplete_or_wrong_response_is_unknown_for_every_dimension(fault):
    def mutate(result):
        if fault == "body_hash":
            result["body_sha256"] = "f" * 64
        elif fault == "prefix_hash":
            result["prefix_sha256"] = "f" * 64
        elif fault == "identity":
            result["answers"]["chapter_number"] = 3
        elif fault == "complete":
            result["coverage_complete"] = False
        elif fault == "truncated_json":
            return json.dumps(result, ensure_ascii=False)[:-1]
        else:
            result["finish_reason"] = "length"
        return result

    result = _review(FormClient(mutate))
    assert set(_statuses(result).values()) == {"unknown"}
    assert result.review.blocking


def test_provider_truncation_metadata_overrides_apparently_valid_json():
    client = FormClient()

    def mutate(result):
        client.llm_attempt_events.append(
            {
                "_raw_response_text": json.dumps(
                    {"choices": [{"finish_reason": "length"}]}
                )
            }
        )
        return result

    client.mutate = mutate
    assert set(_statuses(_review(client)).values()) == {"unknown"}


def test_old_provider_truncation_metadata_does_not_contaminate_new_complete_call():
    client = FormClient()
    client.llm_attempt_events.append(
        {"_raw_response_text": json.dumps({"choices": [{"finish_reason": "length"}]})}
    )
    assert set(_statuses(_review(client)).values()) == {"pass"}


def test_strict_transport_accepts_a_complete_json_string():
    assert set(_statuses(_review(FormClient(json.dumps))).values()) == {"pass"}


@pytest.mark.parametrize(
    "fault",
    ["missing", "wrong_project", "wrong_chapter", "incomplete", "missing_dimension"],
)
def test_incomplete_or_unbound_prefix_never_falls_back_to_old_metadata(fault):
    prefix = _prefix()
    if fault == "missing":
        prefix = {}
    elif fault == "wrong_project":
        prefix["project_id"] = "other"
    elif fault == "wrong_chapter":
        prefix["through_chapter"] = 2
    elif fault == "incomplete":
        prefix["complete"] = False
    else:
        del prefix["facts"]["possession"]
    client = FormClient()
    result = _review(client, prefix)
    assert set(_statuses(result).values()) == {"unknown"}
    assert result.review.blocking
    assert not client.requests


@pytest.mark.parametrize(
    "client",
    [
        None,
        FormClient(
            lambda result: (_ for _ in ()).throw(TimeoutError("model timed out"))
        ),
    ],
)
def test_missing_or_failed_model_returns_unknown(client):
    assert set(_statuses(_review(client)).values()) == {"unknown"}


def test_historical_form_does_not_truncate_twenty_first_signal_or_age_one_signal():
    prefix = _prefix()
    prefix["open_signal_rows"] = [
        {
            "signal_id": f"signal-{index}",
            "description": "尚待核验",
            "chapter_number": 3,
            "status": "open",
        }
        for index in range(25)
    ]
    client = FormClient()
    result = _review(client, prefix)
    assert len(result.review.form.open_signals) == 25
    assert len(client.requests[0]["form"]["open_signals"]) == 25
    assert (
        client.requests[0]["prefix_context"]["open_signal_rows"][-1]["signal_id"]
        == "signal-24"
    )


def test_input_over_budget_is_unknown_without_pruning_or_model_call():
    client = FormClient()
    result = _review(client, max_input_chars=100)
    assert set(_statuses(result).values()) == {"unknown"}
    assert not client.requests


def test_explicitly_truncated_final_body_is_unknown_without_reviewing_partial_text():
    client = FormClient()
    result = _review(client, generation_meta={"finish_reason": "length"})
    assert set(_statuses(result).values()) == {"unknown"}
    assert not client.requests


def test_truncated_input_reported_by_model_cannot_claim_complete_coverage():
    client = FormClient(lambda result: {**result, "input_truncated": True})
    assert set(_statuses(_review(client)).values()) == {"unknown"}


def _critical_prefix(kind):
    prefix = _prefix()
    if kind == "obligation":
        prefix["obligations"] = [
            {"id": "promise", "summary": "开门", "deadline_chapter": 4}
        ]
    elif kind == "countdown":
        prefix["countdown_rows"] = [
            {
                "countdown_key": "clock",
                "status": "active",
                "normalized_remaining_minutes": 30,
            }
        ]
    else:
        prefix["character_rows"] = [
            {
                "character_name": "王松",
                "life_state": "dead" if kind == "terminal_character" else "alive",
                "payload": {"must_track": kind == "must_track_character"},
            }
        ]
    return prefix


@pytest.mark.parametrize(
    ("kind", "dimension"),
    [
        ("obligation", "obligations"),
        ("countdown", "time"),
        ("terminal_character", "life_state"),
        ("must_track_character", "life_state"),
    ],
)
def test_summary_pass_cannot_replace_unknown_critical_tracked_answer(kind, dimension):
    result = _review(FormClient(), _critical_prefix(kind))
    assert _statuses(result)[dimension] == "unknown"
    assert result.review.blocking


def test_low_confidence_critical_answer_does_not_count_as_verified_coverage():
    def mutate(result):
        result["answers"]["obligations"][0]["addressed"] = {
            "value": "fulfilled",
            "confidence": 0.1,
            "evidence_quote": BODY,
        }
        return result

    assert (
        _statuses(_review(FormClient(mutate), _critical_prefix("obligation")))[
            "obligations"
        ]
        == "unknown"
    )


def test_explicit_nonapplicability_needs_full_body_evidence_but_not_unknown_auxiliary_fields():
    def mutate(result):
        character = result["answers"]["characters"][0]
        character["life_state"] = {
            "value": "not_applicable",
            "confidence": 0.95,
            "evidence_quote": BODY,
            "explanation": "完整正文只有林青和城主，王松在本章没有出现或行动，无法从本章确定新的生命状态。",
        }
        character["participation"] = {
            "value": "absent",
            "confidence": 0.95,
            "evidence_quote": BODY,
            "explanation": "完整正文没有王松出场、被提及或行动。",
        }
        # Unknown custody is auxiliary when the prior custody itself is unknown.
        return result

    result = _review(FormClient(mutate), _critical_prefix("must_track_character"))
    assert set(_statuses(result).values()) == {"pass"}


def test_nonapplicability_without_full_body_evidence_is_unknown():
    def mutate(result):
        for field in ("life_state", "participation"):
            result["answers"]["characters"][0][field] = {
                "value": "not_applicable",
                "confidence": 0.95,
                "evidence_quote": BODY[:4],
                "explanation": "本章不适用。",
            }
        return result

    assert (
        _statuses(
            _review(FormClient(mutate), _critical_prefix("must_track_character"))
        )["life_state"]
        == "unknown"
    )


def test_rejected_ordinary_answer_does_not_erase_a_verified_dimension_failure():
    prefix = _prefix()
    prefix["open_signal_rows"] = [
        {"signal_id": "warning", "description": "提示", "severity": "warning"}
    ]

    def mutate(result):
        result["coverage"][0].update(
            status="fail", explanation="前缀已丢失铜钥匙，开门缺乏依据。"
        )
        result["answers"]["open_signals"][0]["resolution_evidence"] = {
            "value": "resolved",
            "confidence": 0.99,
            "evidence_quote": "不存在的引文",
        }
        return result

    result = _review(FormClient(mutate), prefix)
    assert result.review.validation_report.rejected
    assert _statuses(result)["possession"] == "fail"
    assert _statuses(result)["time"] == "unknown"


def test_unknown_noncritical_auxiliary_answers_do_not_mechanically_block_complete_dimensions():
    prefix = _prefix()
    prefix["character_rows"] = [{"character_name": "王松", "life_state": "alive"}]
    prefix["open_signal_rows"] = [
        {"signal_id": "warning", "description": "提示", "severity": "warning"}
    ]
    assert set(_statuses(_review(FormClient(), prefix)).values()) == {"pass"}


def test_historical_generate_json_without_explicit_timeout_support_is_refused():
    class Client:
        called = False

        def generate_json(self, messages, output_schema, temperature, max_tokens):
            self.called = True
            return FormClient().complete_json(messages=messages)

    client = Client()
    assert set(_statuses(_review(client)).values()) == {"unknown"}
    assert not client.called


def test_historical_generate_json_receives_the_supported_timeout():
    class Client:
        timeout = None

        def generate_json(
            self, messages, output_schema, temperature, max_tokens, timeout_seconds=None
        ):
            self.timeout = timeout_seconds
            return FormClient().complete_json(messages=messages)

    client = Client()
    assert set(_statuses(_review(client, timeout_seconds=17)).values()) == {"pass"}
    assert client.timeout == 17


def test_historical_chat_timeout_is_not_hidden_by_automatic_retry():
    class Client:
        def chat(self, messages, *, retry_on_timeout=True, **kwargs):
            if retry_on_timeout:
                return json.dumps(FormClient().complete_json(messages=messages))
            raise TimeoutError("model timed out")

    assert set(_statuses(_review(Client())).values()) == {"unknown"}


@pytest.mark.parametrize("as_json", [False, True])
def test_nonfinite_model_confidence_is_invalid_evidence_not_a_high_confidence_pass(
    as_json,
):
    def mutate(result):
        result["answers"]["obligations"][0]["addressed"] = {
            "value": "fulfilled",
            "confidence": float("inf"),
            "evidence_quote": BODY,
        }
        return json.dumps(result) if as_json else result

    result = _review(FormClient(mutate), _critical_prefix("obligation"))
    assert set(_statuses(result).values()) == {"unknown"}


@pytest.mark.parametrize("kind", ["due_obligation", "critical_signal", "final_crisis"])
def test_required_resolution_cannot_be_waived_by_full_body_nonapplicability(kind):
    prefix = _prefix()
    if kind == "due_obligation":
        prefix = _critical_prefix("obligation")
    elif kind == "critical_signal":
        prefix["open_signal_rows"] = [
            {"signal_id": "critical", "description": "冲突", "severity": "error"}
        ]

    def mutate(result):
        absence = {
            "value": "not_applicable",
            "confidence": 0.99,
            "evidence_quote": BODY,
            "explanation": "正文没有解决这个问题，因此不适用。",
        }
        if kind == "due_obligation":
            result["answers"]["obligations"][0]["addressed"] = absence
        elif kind == "critical_signal":
            result["answers"]["open_signals"][0]["status"] = absence
        else:
            result["answers"]["final_chapter"] = {"main_crisis_status": absence}
        return result

    result = _review(
        FormClient(mutate),
        prefix,
        target_total_chapters=4 if kind == "final_crisis" else 0,
    )
    assert _statuses(result)["obligations"] == "unknown"
    assert result.review.blocking
