from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, TYPE_CHECKING
from collections.abc import Callable, Mapping

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from forwin.generation.gate_delegation import GateAuditWriter

from forwin.audit.events import DecisionEventInfo, DecisionEventType

from .payloads import safe_error_summary
from .redaction import redact_payload


RAW_REQUEST_KEY = "_raw_request_payload"
RAW_RESPONSE_KEY = "_raw_response_text"
PARSE_FAILURE_OUTPUT_KEY = "_parse_failure_output"
_SAFE_PROMPT_ATTEMPT_KEYS = frozenset(
    {
        "attempt_group_id",
        "profile_id",
        "profile_name",
        "model",
        "provider",
        "preferred_provider_kind",
        "preferred_model",
        "base_url_host",
        "requested_temperature",
        "requested_max_tokens",
        "timeout_seconds",
        "attempt_no",
        "http_status",
        "provider_request_id",
        "duration_ms",
        "input_chars",
        "output_chars",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "usage_source",
        "task_family",
        "stage_key",
        "llm_task_route",
        "retry_after",
        "sleep_ms",
        "error_class",
        "error_message",
        "error_category",
        "timeout_kind",
        "retryable",
        "fallback_eligible",
        "final_failure",
        "backend_attempt_group_id",
        "parse_ok",
        "schema_ok",
        "parse_error",
        "parser_name",
        "schema_name",
        "workflow_attempt_no",
        "workflow_retry",
    }
)


def served_model_profile(
    attempts: list[dict[str, object]] | None = None,
    *,
    llm_client: object | None = None,
) -> dict[str, str]:
    """Identify the last served (or failed) attempt, never the configured pin.

    Callers that drain telemetry must pass that call's attempts explicitly.
    A pre-call/scaffold trace has no served model and returns an empty mapping.
    """
    if attempts is None:
        attempts = getattr(llm_client, "llm_attempt_events", [])
    if not isinstance(attempts, (list, tuple)):
        return {}
    for attempt in reversed(attempts):
        if not isinstance(attempt, dict) or not (attempt.get("profile_id") or attempt.get("model")):
            continue
        identity = {
            key: str(attempt[key]) for key in (
                "profile_id", "profile_name", "model", "base_url_host", "provider", "provider_kind",
            ) if attempt.get(key)
        }
        profiles = getattr(llm_client, "profiles", {})
        profile = profiles.get(identity.get("profile_id", ""))
        if profile is not None:
            identity["base_url"] = profile.base_url
        return identity
    return {}


def safe_prompt_trace_attempts(
    attempts: list[dict[str, object]],
    *,
    fallback_attempt_no: int = 0,
    exc: BaseException | None = None,
    duration_ms: int = 0,
) -> list[dict[str, object]]:
    safe_attempts: list[dict[str, object]] = []
    for attempt in attempts:
        safe = {
            key: value
            for key, value in attempt.items()
            if key in _SAFE_PROMPT_ATTEMPT_KEYS and value is not None
        }
        if "error_message" in safe:
            safe["error_message"] = safe_error_summary(
                str(safe.get("error_message") or "")
            )
        safe_attempts.append(safe)
    if not safe_attempts and exc is not None:
        safe_attempts.append(
            {
                "attempt_no": int(fallback_attempt_no or 0),
                "duration_ms": max(0, int(duration_ms or 0)),
                "error_class": exc.__class__.__name__,
                "error_message": safe_error_summary(exc),
                "error_category": "unknown",
                "final_failure": True,
            }
        )
    return safe_attempts


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
    target_group_id = str(
        getattr(llm_client, "llm_attempt_group_id", "") or ""
    )
    for item in reversed(attempts):
        if not isinstance(item, dict):
            continue
        if target_group_id and str(item.get("attempt_group_id") or "") != target_group_id:
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


def mark_latest_attempt_workflow(
    llm_client: object,
    *,
    attempt_no: int,
    stage_key: str = "",
) -> None:
    attempts = getattr(llm_client, "llm_attempt_events", None)
    if not isinstance(attempts, list):
        return
    target_group_id = str(
        getattr(llm_client, "llm_attempt_group_id", "") or ""
    )
    for item in reversed(attempts):
        if not isinstance(item, dict):
            continue
        if target_group_id and str(item.get("attempt_group_id") or "") != target_group_id:
            continue
        if stage_key and str(item.get("stage_key") or "") != str(stage_key):
            continue
        normalized_attempt = max(1, int(attempt_no or 1))
        item["workflow_attempt_no"] = normalized_attempt
        item["workflow_retry"] = normalized_attempt > 1
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
        if (
            bool(attempt.get("workflow_retry"))
            or int(attempt.get("workflow_attempt_no") or 0) > 1
            or int(attempt.get("sleep_ms") or 0) > 0
            or attempt.get("retry_after") is not None
        ):
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
                        "raw_output_preview": str(
                            attempt.get("raw_output_preview") or ""
                        ),
                        "raw_output_artifact_uri": str(
                            attempt.get("raw_output_artifact_uri") or ""
                        ),
                    },
                }
            )
        if (
            str(attempt.get("error_class") or "")
            or bool(attempt.get("final_failure"))
            or has_parse_error
        ):
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
        next_attempt = (
            dict_attempts[index + 1] if index + 1 < len(dict_attempts) else None
        )
        if (
            next_attempt is not None
            and str(next_attempt.get("attempt_group_id") or "")
            == str(attempt.get("attempt_group_id") or "")
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
                        "reason": str(
                            attempt.get("error_message")
                            or attempt.get("error_category")
                            or ""
                        ),
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
                content_type="application/json"
                if response_text.strip().startswith(("{", "["))
                else "text/plain; charset=utf-8",
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


def _event_attempt_payload(
    attempt: dict[str, Any], *, prompt_trace_id: str
) -> dict[str, Any]:
    keys = {
        "attempt_group_id",
        "attempt_no",
        "workflow_attempt_no",
        "workflow_retry",
        "profile_id",
        "profile_name",
        "model",
        "provider",
        "base_url_host",
        "stage_key",
        "llm_task_route",
        "http_status",
        "provider_request_id",
        "duration_ms",
        "input_chars",
        "output_chars",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "usage_source",
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
                        **{
                            key: value
                            for key, value in item.items()
                            if key != "content"
                        },
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


_POST_CANON_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def post_canon_trace_artifact_key(
    *,
    canon_commit_id: str,
    step_name: str,
) -> str:
    commit_id = str(canon_commit_id or "").strip()
    step = str(step_name or "").strip()
    if not _POST_CANON_ID_RE.fullmatch(commit_id):
        raise ValueError("canon_commit_id is not safe for an artifact key")
    if not _POST_CANON_ID_RE.fullmatch(step):
        raise ValueError("step_name is not safe for an artifact key")
    return f"post_canon/{commit_id}/{step}/llm_trace.json"


def drain_workflow_attempts(
    llm_client: object, *, attempt_no: int, stage_key: str, task_family: str,
    messages: list[dict], raw: str, call_error: Exception | None, error_message: str,
    replace_error_message: bool = False,
    parse_error: Exception | None = None, parser_name: str = "", schema_name: str = "",
) -> list[dict[str, Any]]:
    """Drain one workflow attempt, retaining provider details and the caller's error policy."""
    if parse_error is not None:
        mark_latest_attempt_parse_failure(
            llm_client, parser_name=parser_name, stage_key=stage_key,
            schema_name=schema_name, raw_output=raw, error=parse_error,
        )
    mark_latest_attempt_workflow(llm_client, attempt_no=attempt_no, stage_key=stage_key)
    drain = getattr(llm_client, "drain_llm_attempt_events", None)
    drained = drain() if callable(drain) else []
    attempts = drained if isinstance(drained, list) and drained else [{
        "attempt_no": 1, "stage_key": stage_key, "task_family": task_family,
    }]
    for event in attempts:
        event.setdefault("workflow_attempt_no", attempt_no)
        event.setdefault("workflow_retry", attempt_no > 1)
        event.setdefault(RAW_REQUEST_KEY, {"messages": messages})
        event.setdefault(RAW_RESPONSE_KEY, raw)
        if call_error is not None:
            event.setdefault("error_class", type(call_error).__name__)
            if replace_error_message:
                event["error_message"] = error_message
            else:
                event.setdefault("error_message", error_message)
    return attempts


def build_workflow_trace(
    llm_client: object, *, messages: list[dict], attempts: list[dict[str, Any]],
    trace_scope: str, stage_key: str, template_id: str,
    input_snapshot: dict[str, Any], output_summary: dict[str, Any],
) -> dict[str, Any]:
    """Build the common trace envelope while retaining role-specific input and output fields."""
    from forwin.skills import serialize_prompt_layers

    last_call_result = getattr(llm_client, "last_call_result", None) if attempts else None
    trace = getattr(last_call_result, "trace", {}) or {}
    layers = serialize_prompt_layers(messages, [])
    return {
        "trace_scope": trace_scope, "stage_key": stage_key,
        "backend": str(trace.get("backend") or getattr(last_call_result, "backend", "") or ""),
        "codex_job_id": str(trace.get("codex_job_id", "") or ""),
        "permission_profile": str(trace.get("permission_profile", "") or ""),
        "fallback_used": bool(getattr(last_call_result, "fallback_used", False)),
        "template_id": template_id, "template_version": "v1",
        "effective_system_prompt": "\n\n".join(str(item["content"]).strip() for item in layers if item["role"] == "system"),
        "prompt_layers": layers,
        "input_snapshot": input_snapshot,
        "model_profile": {
            **{key: getattr(llm_client, key, "") for key in ("profile_id", "profile_name", "model", "base_url")},
            "actual_model": getattr(last_call_result, "actual_model", "") or "",
        },
        "attempts": attempts,
        "output_summary": output_summary,
    }


def own_transaction_audit_writer(
    session_factory: Callable[[], Session],
) -> Callable[[], GateAuditWriter]:
    """Audit writers whose every write commits on its own, so it outlives a world step that rolls back."""
    from forwin.generation.gate_delegation import GateAuditWriter
    from forwin.models.project import Project
    from forwin.state.updater import StateUpdater

    def save_decision_event(info: DecisionEventInfo) -> Any:
        with session_factory.begin() as session:
            row = StateUpdater(session).save_decision_event(info)
            session.expunge(row)
            return row

    def save_prompt_trace(**payload: Any) -> Any:
        with session_factory.begin() as session:
            project = session.get(Project, payload["project_id"])
            payload.setdefault("genesis_revision_id", str(getattr(project, "active_genesis_revision_id", "") or ""))
            row = StateUpdater(session).save_prompt_trace(**payload)
            session.expunge(row)
            return row

    def factory() -> GateAuditWriter:
        return GateAuditWriter(
            save_decision_event=save_decision_event,
            save_prompt_trace=save_prompt_trace,
        )

    return factory


def save_world_prompt_trace(
    session: Session,
    prompt_trace: Mapping[str, Any],
    *,
    artifact_store: Any = None,
    independent: bool = False,
) -> str:
    """Save a role's trace and events, optionally through the independent audit writer."""
    from sqlalchemy.orm import sessionmaker
    from forwin.models.project import Project
    from forwin.state.updater import StateUpdater

    if not prompt_trace:
        return ""
    if independent and not prompt_trace.get("attempts"):
        # No model attempt needs preserving before the caller's parent rows commit.
        return ""
    snapshot = prompt_trace.get("input_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    project_id = str(snapshot.get("project_id") or "")
    chapter_number = int(snapshot.get("chapter_number") or 0)
    payload = prepare_prompt_trace_payload(
        dict(prompt_trace),
        artifact_store=artifact_store,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    if independent:
        updater = own_transaction_audit_writer(sessionmaker(bind=session.get_bind().engine, expire_on_commit=False))()
        revision = {}
    else:
        project = session.get(Project, project_id)
        updater = StateUpdater(session)
        revision = {"genesis_revision_id": str(getattr(project, "active_genesis_revision_id", "") or "")}
    row = updater.save_prompt_trace(
        project_id=project_id,
        **revision,
        trace_scope=str(payload.get("trace_scope") or "world_engine"),
        stage_key=str(payload.get("stage_key") or ""),
        template_id=str(payload.get("template_id") or ""),
        template_version=str(payload.get("template_version") or "v1"),
        effective_system_prompt=str(payload.get("effective_system_prompt") or ""),
        prompt_layers_json=json.dumps(payload.get("prompt_layers", []), ensure_ascii=False),
        input_snapshot_json=json.dumps(payload.get("input_snapshot", {}), ensure_ascii=False),
        model_profile_json=json.dumps(payload.get("model_profile", {}), ensure_ascii=False),
        attempts_json=json.dumps(payload.get("attempts", []), ensure_ascii=False),
        output_summary_json=json.dumps(payload.get("output_summary", {}), ensure_ascii=False),
        backend=str(payload.get("backend") or ""),
        codex_job_id=str(payload.get("codex_job_id") or ""),
        permission_profile=str(payload.get("permission_profile") or ""),
        fallback_used=bool(payload.get("fallback_used", False)),
    )
    for event in build_llm_decision_event_payloads(payload, prompt_trace_id=row.id):
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project_id,
                chapter_number=chapter_number,
                scope="chapter" if chapter_number else "project",
                event_family=str(event.get("event_family") or "runtime_observation"),
                event_type=str(event.get("event_type") or DecisionEventType.LLM_REQUEST_FAILED),
                actor_type="system",
                summary=str(event.get("summary") or "World engine LLM trace event."),
                payload=event.get("payload") if isinstance(event.get("payload"), dict) else {},
                related_object_type="prompt_trace",
                related_object_id=row.id,
            )
        )
    return row.id
