from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.fallbacks import *
from forwin.book_genesis_core.names_paths import *

def _generate_stage_payload(
    self,
    *,
    project: Project,
    pack: dict[str, Any],
    stage_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if stage_key == "brief":
        fallback = _fallback_brief(project, pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {})
    elif stage_key == "world":
        fallback = _fallback_world(project, pack)
    elif stage_key == "map":
        fallback = _fallback_map(pack)
    elif stage_key == "story_engine":
        fallback = _fallback_story_engine(pack)
    elif stage_key == "book_blueprint":
        fallback = _fallback_blueprint(project, pack)
    elif stage_key == "bootstrap":
        fallback = _fallback_bootstrap(project, pack)
    else:
        raise ValueError(f"未知 Genesis stage: {stage_key}")
    messages = self._build_stage_generation_messages(
        project=project,
        pack=pack,
        stage_key=stage_key,
        fallback=fallback,
    )
    payload, trace = self._call_json_with_trace(messages=messages, fallback=fallback, stage_key=stage_key)
    if stage_key == "book_blueprint":
        payload = self._normalize_blueprint_payload(project=project, payload=payload, fallback=fallback)
    elif stage_key == "world":
        payload = self._normalize_world_root_payload(project=project, payload=payload, fallback=fallback)
    elif stage_key == "map":
        payload = self._normalize_map_payload(
            payload=payload,
            fallback=fallback,
            world_bible=_pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {},
        )
    elif stage_key == "story_engine":
        payload = self._normalize_story_engine_payload(
            payload=payload,
            fallback=fallback,
            world_bible=_pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {},
            map_atlas=_pack_stage_payload(pack, "world").get("map_atlas") if isinstance(_pack_stage_payload(pack, "world").get("map_atlas"), dict) else {},
        )
    return payload, trace

def _refine_stage_payload(
    self,
    *,
    project: Project,
    pack: dict[str, Any],
    stage_key: str,
    instruction: str,
    target_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_target_path = _normalize_stage_target_path(stage_key, target_path)
    current_payload = _pack_stage_payload(pack, stage_key)
    support_context = self._refine_support_context(pack=pack, stage_key=stage_key)
    fallback_stage_payload = current_payload or (
        _fallback_map(pack) if stage_key == "map"
        else _fallback_story_engine(pack) if stage_key == "story_engine"
        else _fallback_world(project, pack) if stage_key == "world"
        else _fallback_brief(project, pack.get("book_brief") if isinstance(pack.get("book_brief"), dict) else {})
        if stage_key == "brief"
        else _fallback_blueprint(project, pack) if stage_key == "book_blueprint"
        else _fallback_bootstrap(project, pack)
    )
    if normalized_target_path:
        source_payload = current_payload if current_payload else fallback_stage_payload
        current_target = _get_value_at_path(source_payload, normalized_target_path)
        if isinstance(current_target, dict):
            fallback = _json_clone(current_target)
            messages = self._build_stage_refine_messages(
                pack=pack,
                stage_key=stage_key,
                instruction=instruction,
                target_path=target_path,
                current_payload=current_payload,
                support_context=support_context,
                fallback_stage_payload=fallback_stage_payload,
                current_target=current_target,
            )
            payload, trace = self._call_json_with_trace(
                messages=messages,
                fallback=fallback,
                stage_key=f"{stage_key}:refine_item",
                max_tokens=1400,
            )
        else:
            wrapped_fallback = {"value": _json_clone(current_target)}
            messages = self._build_stage_refine_messages(
                pack=pack,
                stage_key=stage_key,
                instruction=instruction,
                target_path=target_path,
                current_payload=current_payload,
                support_context=support_context,
                fallback_stage_payload=fallback_stage_payload,
                current_target=current_target,
                wrap_scalar_value=True,
            )
            payload, trace = self._call_json_with_trace(
                messages=messages,
                fallback=wrapped_fallback,
                stage_key=f"{stage_key}:refine_item",
                max_tokens=1400,
            )
            payload = payload.get("value", wrapped_fallback["value"]) if isinstance(payload, dict) else wrapped_fallback["value"]
        next_payload = _json_clone(current_payload)
        _set_value_at_path(next_payload, normalized_target_path, payload)
    else:
        messages = self._build_stage_refine_messages(
            pack=pack,
            stage_key=stage_key,
            instruction=instruction,
            target_path="",
            current_payload=current_payload,
            support_context=support_context,
            fallback_stage_payload=fallback_stage_payload,
        )
        payload, trace = self._call_json_with_trace(
            messages=messages,
            fallback=fallback_stage_payload,
            stage_key=f"{stage_key}:refine",
            max_tokens=1800,
        )
        next_payload = payload

    if stage_key == "book_blueprint":
        next_payload = self._normalize_blueprint_payload(
            project=project,
            payload=next_payload if isinstance(next_payload, dict) else {},
            fallback=_fallback_blueprint(project, pack),
        )
    elif stage_key == "world":
        next_payload = self._normalize_world_root_payload(
            project=project,
            payload=next_payload if isinstance(next_payload, dict) else {},
            fallback=_fallback_world(project, pack),
        )
    elif stage_key == "map":
        next_payload = self._normalize_map_payload(
            payload=next_payload if isinstance(next_payload, dict) else {},
            fallback=_fallback_map(pack),
            world_bible=_pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {},
        )
    elif stage_key == "story_engine":
        next_payload = self._normalize_story_engine_payload(
            payload=next_payload if isinstance(next_payload, dict) else {},
            fallback=_fallback_story_engine(pack),
            world_bible=_pack_stage_payload(pack, "world").get("world_bible") if isinstance(_pack_stage_payload(pack, "world").get("world_bible"), dict) else {},
            map_atlas=_pack_stage_payload(pack, "world").get("map_atlas") if isinstance(_pack_stage_payload(pack, "world").get("map_atlas"), dict) else {},
        )
    elif not isinstance(next_payload, dict):
        next_payload = fallback_stage_payload

    trace["input_snapshot"] = {
        **(trace.get("input_snapshot") if isinstance(trace.get("input_snapshot"), dict) else {}),
        "instruction": instruction,
        "target_path": target_path,
        "current_stage_payload": current_payload,
    }
    trace["output_summary"] = {
        **(trace.get("output_summary") if isinstance(trace.get("output_summary"), dict) else {}),
        "instruction": instruction,
        "target_path": target_path,
        "normalized_target_path": normalized_target_path,
    }
    return next_payload, trace

def _call_json_with_trace(
    self,
    *,
    messages: list[dict[str, str]],
    fallback: dict[str, Any],
    stage_key: str,
    temperature: float = 0.45,
    max_tokens: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return self.trace_service.call_json_with_trace(
        messages=messages,
        fallback=fallback,
        stage_key=stage_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )

def _call_json_with_trace_impl(
    self,
    *,
    messages: list[dict[str, str]],
    fallback: dict[str, Any],
    stage_key: str,
    temperature: float = 0.45,
    max_tokens: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    skill_selections, skill_layers = self._resolve_skill_layers(stage_key=stage_key)
    effective_messages = inject_skill_layers(messages, skill_layers)
    prompt_layers = serialize_prompt_layers(messages, skill_layers)
    selected_skills = summarize_selected_skills(skill_selections)
    effective_prompt = "\n\n".join(
        str(item.get("content", "")).strip()
        for item in effective_messages
        if str(item.get("role", "")).strip() == "system"
    )
    attempts_payload: list[dict[str, Any]] = []
    max_tokens = min(self.max_tokens, int(max_tokens or self.max_tokens))
    if (
        hasattr(self.llm_client, "api_key")
        and not str(getattr(self.llm_client, "api_key", "") or "").strip()
        and not bool(getattr(self.llm_client, "codex_enabled", False))
    ):
        attempts_payload.append({"status": "fallback", "reason": "missing_api_key"})
        return fallback, self._trace_payload(
            stage_key=stage_key,
            effective_system_prompt=effective_prompt,
            messages=messages,
            prompt_layers=prompt_layers,
            selected_skills=selected_skills,
            attempts=attempts_payload,
            output_summary={"mode": "fallback", "payload": fallback},
        )
    retry_plan = [
        {"temperature": temperature, "max_tokens": max_tokens},
        {"temperature": max(0.2, temperature - 0.15), "max_tokens": max(480, min(max_tokens, 900))},
    ]
    for attempt_no, attempt in enumerate(retry_plan, start=1):
        try:
            is_chapter_plan = str(stage_key or "").startswith("launch_arc_")
            raw = self._call_llm_chat(
                effective_messages,
                temperature=attempt["temperature"],
                max_tokens=attempt["max_tokens"],
                response_format={"type": "json_object"},
                task_family="chapter_plan_materialization" if is_chapter_plan else "genesis",
                stage_key=stage_key,
                codex_allowed=not is_chapter_plan,
                output_schema={"type": "object"},
            )
            try:
                payload = parse_llm_json(raw, error_prefix=f"Genesis {stage_key}")
            except Exception as exc:  # noqa: BLE001
                mark_latest_attempt_parse_failure(
                    self.llm_client,
                    parser_name=f"Genesis {stage_key}",
                    stage_key=stage_key,
                    schema_name=f"genesis:{stage_key}",
                    raw_output=raw,
                    error=exc,
                )
                raise
            attempts_payload.append(
                {
                    "attempt": attempt_no,
                    "status": "success",
                    "temperature": attempt["temperature"],
                    "max_tokens": attempt["max_tokens"],
                }
            )
            return payload, self._trace_payload(
                stage_key=stage_key,
                effective_system_prompt=effective_prompt,
                messages=messages,
                prompt_layers=prompt_layers,
                selected_skills=selected_skills,
                attempts=attempts_payload,
                output_summary={"mode": "success", "payload": payload},
            )
        except Exception as exc:  # noqa: BLE001
            attempts_payload.append(
                {
                    "attempt": attempt_no,
                    "status": "failed",
                    "temperature": attempt["temperature"],
                    "max_tokens": attempt["max_tokens"],
                    "error": str(exc),
                }
            )
            if isinstance(exc, LLMJSONParseError) and exc.empty_response:
                break
    logger.warning("Genesis stage %s fell back to deterministic scaffold.", stage_key)
    attempts_payload.append({"status": "fallback", "reason": "deterministic_scaffold"})
    return fallback, self._trace_payload(
        stage_key=stage_key,
        effective_system_prompt=effective_prompt,
        messages=messages,
        prompt_layers=prompt_layers,
        selected_skills=selected_skills,
        attempts=attempts_payload,
        output_summary={"mode": "fallback", "payload": fallback},
    )

def _call_llm_chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
    signature = inspect.signature(self.llm_client.chat)
    parameters = signature.parameters
    accepts_var_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    filtered = {
        key: value
        for key, value in kwargs.items()
        if accepts_var_kwargs or key in parameters
    }
    return self.llm_client.chat(messages, **filtered)

def _resolve_skill_layers(self, *, stage_key: str):
    if self.skill_router is None or self.skill_prompt_layer_builder is None:
        return [], []
    normalized_stage_key = str(stage_key or "").strip()
    selection_stage_key = normalized_stage_key
    task_family = "generate_stage_payload"
    if normalized_stage_key.startswith("launch_arc_"):
        selection_stage_key = "book_blueprint"
        task_family = "launch_arc_plan"
    elif ":refine" in normalized_stage_key:
        selection_stage_key = normalized_stage_key.split(":", 1)[0]
        task_family = "refine_stage_payload"
    selections = self.skill_router.select(
        scope="genesis",
        stage_key=selection_stage_key,
        task_family=task_family,
    )
    return selections, self.skill_prompt_layer_builder.build(selections)

def _trace_payload(
    self,
    *,
    stage_key: str,
    effective_system_prompt: str,
    messages: list[dict[str, str]],
    prompt_layers: list[dict[str, Any]] | None = None,
    selected_skills: list[dict[str, str]] | None = None,
    attempts: list[dict[str, Any]],
    output_summary: dict[str, Any],
) -> dict[str, Any]:
    selected = list(selected_skills or [])
    drain_attempts = getattr(self.llm_client, "drain_llm_attempt_events", None)
    llm_attempts = drain_attempts() if callable(drain_attempts) else []
    business_attempts = [
        {"attempt_type": "business", **dict(item)}
        for item in attempts
        if isinstance(item, dict)
    ]
    last_call_result = getattr(self.llm_client, "last_call_result", None)
    trace = getattr(last_call_result, "trace", {}) if last_call_result is not None else {}
    backend = str(trace.get("backend", "") or getattr(last_call_result, "backend", "") or "")
    permission_profile = str(trace.get("permission_profile", "") or "")
    fallback_used = bool(getattr(last_call_result, "fallback_used", False)) if last_call_result is not None else False
    return {
        "backend": backend,
        "codex_job_id": str(trace.get("codex_job_id", "") or ""),
        "permission_profile": permission_profile,
        "fallback_used": fallback_used,
        "effective_system_prompt": effective_system_prompt,
        "prompt_layers": prompt_layers
        if prompt_layers is not None
        else [
            {"role": str(item.get("role", "")).strip(), "content": str(item.get("content", ""))}
            for item in messages
        ],
        "input_snapshot": {
            "stage_key": stage_key,
            "messages": messages,
            "selected_skills": selected,
        },
        "model_profile": {
            "profile_id": getattr(self.llm_client, "profile_id", ""),
            "profile_name": getattr(self.llm_client, "profile_name", ""),
            "model": getattr(self.llm_client, "model", ""),
            "base_url": getattr(self.llm_client, "base_url", ""),
        },
        "attempts": (llm_attempts if isinstance(llm_attempts, list) else []) + business_attempts,
        "output_summary": {
            **output_summary,
            "skill_summary": selected,
            "business_attempts": attempts,
        },
    }

def _prepare_trace_payload_for_save(
    self,
    trace_payload: dict[str, Any],
    *,
    project_id: str,
) -> dict[str, Any]:
    return prepare_prompt_trace_payload(
        trace_payload,
        artifact_store=getattr(self, "artifact_store", None),
        project_id=project_id,
    )

def _record_llm_events_for_trace(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    trace_id: str,
    trace_payload: dict[str, Any],
    decision_event_id: str = "",
) -> None:
    for event_payload in build_llm_decision_event_payloads(trace_payload, prompt_trace_id=trace_id):
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project_id,
                scope="project",
                event_family=str(event_payload.get("event_family") or "runtime_observation"),
                event_type=str(event_payload.get("event_type") or DecisionEventType.LLM_REQUEST_FAILED),
                actor_type="system",
                summary=str(event_payload.get("summary") or "Genesis LLM trace event."),
                payload=event_payload.get("payload") if isinstance(event_payload.get("payload"), dict) else {},
                related_object_type="prompt_trace",
                related_object_id=trace_id,
                parent_event_id=str(decision_event_id or ""),
            )
        )
    self._record_trace_performance_spans(
        project_id=project_id,
        trace_id=trace_id,
        trace_payload=trace_payload,
    )

def _record_trace_performance_spans(
    self,
    *,
    project_id: str,
    trace_id: str,
    trace_payload: dict[str, Any],
) -> None:
    attempts = trace_payload.get("attempts") if isinstance(trace_payload, dict) else []
    if not isinstance(attempts, list):
        return
    trace_scope = str(trace_payload.get("trace_scope") or "genesis").strip() or "genesis"
    fallback_stage = str(trace_payload.get("stage_key") or "").strip()
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        stage_key = str(attempt.get("stage_key") or fallback_stage or "").strip()
        duration_ms = max(0, int(attempt.get("duration_ms") or 0))
        failed = bool(attempt.get("error_class") or attempt.get("final_failure") or attempt.get("parse_error"))
        record = SpanRecord(
            context=OperationContext(
                project_id=project_id,
                stage=stage_key,
                operation_id=trace_id,
            ),
            span_name="llm.request",
            span_kind="llm",
            component=trace_scope,
            tags=redact_payload(
                {
                    "prompt_trace_id": trace_id,
                    "trace_scope": trace_scope,
                    "stage_key": stage_key,
                    "profile_id": str(attempt.get("profile_id") or ""),
                    "model": str(attempt.get("model") or ""),
                    "llm_task_route": str(attempt.get("llm_task_route") or ""),
                    "attempt_no": int(attempt.get("attempt_no") or 0),
                    "attempt_group_id": str(attempt.get("attempt_group_id") or ""),
                }
            ),
            metrics={
                "input_chars": int(attempt.get("input_chars") or 0),
                "output_chars": int(attempt.get("output_chars") or 0),
                "sleep_ms": int(attempt.get("sleep_ms") or 0),
            },
            status="failed" if failed else "ok",
            error=redact_payload(
                {
                    "error_class": str(attempt.get("error_class") or ""),
                    "error_message": str(attempt.get("error_message") or attempt.get("parse_error") or ""),
                    "error_category": str(attempt.get("error_category") or ""),
                }
            )
            if failed
            else {},
            trace_id=trace_id,
            span_id=uuid4().hex,
            parent_span_id="",
            start_time_unix_ms=int(time.time() * 1000),
            duration_ms=duration_ms,
            self_duration_ms=duration_ms,
        )
        try:
            self.observability._record_span(record)
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring Genesis LLM performance span failure.", exc_info=True)



__all__ = ['_generate_stage_payload', '_refine_stage_payload', '_call_json_with_trace', '_call_json_with_trace_impl', '_call_llm_chat', '_resolve_skill_layers', '_trace_payload', '_prepare_trace_payload_for_save', '_record_llm_events_for_trace', '_record_trace_performance_spans']
