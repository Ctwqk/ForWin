from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from forwin.governance import DecisionEventType

from .redaction import redact_payload


RAW_REQUEST_KEY = "_raw_request_payload"
RAW_RESPONSE_KEY = "_raw_response_text"
PARSE_FAILURE_OUTPUT_KEY = "_parse_failure_output"


def mark_latest_attempt_parse_failure(
    llm_client: object,
    *,
    parser_name: str,
    stage_key: str = "",
    schema_name: str = "",
    raw_output: str = "",
    error: BaseException | str = "",
) -> None:
    attempts = getattr(llm_client, "llm_attempt_events", None)
    if not isinstance(attempts, list):
        return
    for item in reversed(attempts):
        if not isinstance(item, dict):
            continue
        if stage_key and str(item.get("stage_key") or "") != str(stage_key):
            continue
        item["parse_ok"] = False
        item["schema_ok"] = False
        item["parse_error"] = str(error)
        item["parser_name"] = str(parser_name or "")
        item["schema_name"] = str(schema_name or "")
        item[PARSE_FAILURE_OUTPUT_KEY] = str(raw_output or "")
        item["error_category"] = str(item.get("error_category") or "parse_error")
        return


def prepare_prompt_trace_payload(
    prompt_trace: dict[str, Any],
    *,
    artifact_store: object | None = None,
    project_id: str = "",
    chapter_number: int = 0,
) -> dict[str, Any]:
    payload = copy.deepcopy(prompt_trace if isinstance(prompt_trace, dict) else {})
    attempts = payload.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    prepared_attempts = [
        _prepare_attempt(
            item,
            artifact_store=artifact_store,
            project_id=project_id,
            chapter_number=chapter_number,
            trace_scope=str(payload.get("trace_scope", "llm") or "llm"),
            stage_key=str(payload.get("stage_key", "") or ""),
        )
        for item in attempts
        if isinstance(item, dict)
    ]
    payload["attempts"] = prepared_attempts
    output_summary = payload.get("output_summary")
    if not isinstance(output_summary, dict):
        output_summary = {}
    output_summary["drained_attempt_count"] = len(prepared_attempts)
    payload["output_summary"] = output_summary
    return payload


def build_llm_decision_event_payloads(
    prompt_trace: dict[str, Any],
    *,
    prompt_trace_id: str = "",
) -> list[dict[str, Any]]:
    attempts = prompt_trace.get("attempts") if isinstance(prompt_trace, dict) else []
    if not isinstance(attempts, list):
        return []
    events: list[dict[str, Any]] = []
    dict_attempts = [item for item in attempts if isinstance(item, dict)]
    for index, attempt in enumerate(dict_attempts):
        base_payload = _event_attempt_payload(attempt, prompt_trace_id=prompt_trace_id)
        stage = str(attempt.get("stage_key") or prompt_trace.get("stage_key") or "")
        model = str(attempt.get("model") or "")
        has_parse_error = bool(attempt.get("parse_error"))
        if int(attempt.get("sleep_ms") or 0) > 0 or attempt.get("retry_after") is not None:
            events.append(
                {
                    "event_family": "runtime_observation",
                    "event_type": DecisionEventType.RETRY_ATTEMPT,
                    "summary": f"LLM retry scheduled for {stage or 'unknown stage'} on {model or 'unknown model'}.",
                    "payload": {
                        **base_payload,
                        "retry_after": attempt.get("retry_after"),
                        "sleep_ms": int(attempt.get("sleep_ms") or 0),
                    },
                }
            )
        if attempt.get("parse_error"):
            events.append(
                {
                    "event_family": "runtime_observation",
                    "event_type": DecisionEventType.LLM_RESPONSE_PARSE_FAILED,
                    "summary": f"LLM response parse failed for {stage or 'unknown stage'} on {model or 'unknown model'}.",
                    "payload": {
                        **base_payload,
                        "parser_name": str(attempt.get("parser_name") or ""),
                        "schema_name": str(attempt.get("schema_name") or ""),
                        "parse_error": str(attempt.get("parse_error") or ""),
                        "raw_output_preview": str(attempt.get("raw_output_preview") or ""),
                        "raw_output_artifact_uri": str(attempt.get("raw_output_artifact_uri") or ""),
                    },
                }
            )
        if str(attempt.get("error_class") or "") or bool(attempt.get("final_failure")) or has_parse_error:
            events.append(
                {
                    "event_family": "runtime_observation",
                    "event_type": DecisionEventType.LLM_REQUEST_FAILED,
                    "summary": f"LLM request failed for {stage or 'unknown stage'} on {model or 'unknown model'}.",
                    "payload": base_payload,
                }
            )
        elif int(attempt.get("output_chars") or 0) > 0:
            events.append(
                {
                    "event_family": "runtime_observation",
                    "event_type": DecisionEventType.LLM_REQUEST_SUCCEEDED,
                    "summary": f"LLM request succeeded for {stage or 'unknown stage'} on {model or 'unknown model'}.",
                    "payload": base_payload,
                }
            )
        next_attempt = dict_attempts[index + 1] if index + 1 < len(dict_attempts) else None
        if (
            next_attempt is not None
            and str(next_attempt.get("attempt_group_id") or "") == str(attempt.get("attempt_group_id") or "")
            and str(next_attempt.get("model") or "") != str(attempt.get("model") or "")
            and (attempt.get("fallback_eligible") or attempt.get("final_failure"))
        ):
            events.append(
                {
                    "event_family": "runtime_observation",
                    "event_type": DecisionEventType.FALLBACK_PROFILE_SWITCHED,
                    "summary": (
                        f"LLM fallback: {str(attempt.get('model') or '-')} -> "
                        f"{str(next_attempt.get('model') or '-')}"
                    ),
                    "payload": {
                        **base_payload,
                        "from_profile_id": str(attempt.get("profile_id") or ""),
                        "from_model": str(attempt.get("model") or ""),
                        "to_profile_id": str(next_attempt.get("profile_id") or ""),
                        "to_model": str(next_attempt.get("model") or ""),
                        "reason": str(attempt.get("error_message") or attempt.get("error_category") or ""),
                    },
                }
            )
    return events


def _prepare_attempt(
    attempt: dict[str, Any],
    *,
    artifact_store: object | None,
    project_id: str,
    chapter_number: int,
    trace_scope: str,
    stage_key: str,
) -> dict[str, Any]:
    prepared = {
        str(key): value
        for key, value in attempt.items()
        if not str(key).startswith("_")
    }
    raw_request = attempt.get(RAW_REQUEST_KEY)
    raw_response = attempt.get(RAW_RESPONSE_KEY)
    parse_output = attempt.get(PARSE_FAILURE_OUTPUT_KEY)
    attempt_stage = str(prepared.get("stage_key") or stage_key or "")
    group_id = str(prepared.get("attempt_group_id") or "")
    attempt_no = int(prepared.get("attempt_no") or 0)
    if raw_request is not None:
        request_text = _json_dumps(raw_request)
        prepared["request_hash"] = _hash_text(request_text)
        if artifact_store is not None and project_id:
            raw_meta = _save_llm_artifact(
                artifact_store,
                project_id=project_id,
                artifact_kind="raw_prompt",
                content=request_text,
                trace_scope=trace_scope,
                stage_key=attempt_stage,
                chapter_number=chapter_number,
                attempt_group_id=group_id,
                attempt_no=attempt_no,
                content_type="application/json",
            )
            redacted_text = _json_dumps(_redacted_request_payload(raw_request))
            redacted_meta = _save_llm_artifact(
                artifact_store,
                project_id=project_id,
                artifact_kind="redacted_request",
                content=redacted_text,
                trace_scope=trace_scope,
                stage_key=attempt_stage,
                chapter_number=chapter_number,
                attempt_group_id=group_id,
                attempt_no=attempt_no,
                content_type="application/json",
            )
            prepared["request_artifact_uri"] = raw_meta["artifact_uri"]
            prepared["raw_prompt_artifact_uri"] = raw_meta["artifact_uri"]
            prepared["redacted_request_artifact_uri"] = redacted_meta["artifact_uri"]
            prepared["request_size"] = raw_meta["size"]
    response_source = parse_output if parse_output is not None else raw_response
    if response_source is not None:
        response_text = str(response_source or "")
        prepared["response_hash"] = _hash_text(response_text)
        prepared["response_preview"] = _preview(response_text)
        if artifact_store is not None and project_id:
            kind = (
                "parse_failure_output"
                if parse_output is not None
                else "error_response"
                if str(prepared.get("error_class") or "")
                else "raw_response"
            )
            response_meta = _save_llm_artifact(
                artifact_store,
                project_id=project_id,
                artifact_kind=kind,
                content=response_text,
                trace_scope=trace_scope,
                stage_key=attempt_stage,
                chapter_number=chapter_number,
                attempt_group_id=group_id,
                attempt_no=attempt_no,
                content_type="application/json" if response_text.strip().startswith(("{", "[")) else "text/plain; charset=utf-8",
            )
            prepared["response_artifact_uri"] = response_meta["artifact_uri"]
            prepared["raw_response_artifact_uri"] = response_meta["artifact_uri"]
            prepared["response_size"] = response_meta["size"]
            if parse_output is not None:
                prepared["raw_output_artifact_uri"] = response_meta["artifact_uri"]
                prepared["raw_output_preview"] = response_meta["preview"]
    return redact_payload(prepared)


def _save_llm_artifact(artifact_store: object, **kwargs: Any) -> dict[str, Any]:
    save = getattr(artifact_store, "save_llm_artifact", None)
    if not callable(save):
        return {}
    return dict(save(**kwargs) or {})


def _event_attempt_payload(attempt: dict[str, Any], *, prompt_trace_id: str) -> dict[str, Any]:
    keys = {
        "attempt_group_id",
        "attempt_no",
        "profile_id",
        "profile_name",
        "model",
        "base_url_host",
        "stage_key",
        "llm_task_route",
        "http_status",
        "provider_request_id",
        "duration_ms",
        "input_chars",
        "output_chars",
        "error_category",
        "error_class",
        "error_message",
        "timeout_kind",
        "retryable",
        "fallback_eligible",
        "final_failure",
        "request_artifact_uri",
        "response_artifact_uri",
        "request_hash",
        "response_hash",
        "response_preview",
        "route_policy_version",
        "candidate_chain",
        "skipped_profiles",
    }
    payload = {key: attempt.get(key) for key in keys if key in attempt}
    payload["prompt_trace_id"] = prompt_trace_id
    return payload


def _redacted_request_payload(raw_request: Any) -> Any:
    payload = copy.deepcopy(raw_request)
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            redacted_messages: list[dict[str, Any]] = []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "")
                redacted_messages.append(
                    {
                        **{key: value for key, value in item.items() if key != "content"},
                        "content_preview": _preview(content, limit=180),
                        "content_hash": _hash_text(content),
                        "content_chars": len(content),
                    }
                )
            payload["messages"] = redacted_messages
    return redact_payload(payload)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _preview(text: str, *, limit: int = 500) -> str:
    return " ".join(str(text or "").split())[: max(1, int(limit or 500))]
