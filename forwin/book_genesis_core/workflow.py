from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.fallbacks import *
from forwin.book_genesis_core.names_paths import *

def create_initial_revision(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    brief_seed: dict[str, Any] | None = None,
):
    return self.workspace.create_initial_revision(
        session=session,
        updater=updater,
        project=project,
        brief_seed=brief_seed,
    )
    pack = _initial_pack(project, brief_seed)
    row = updater.create_book_genesis_revision(
        project_id=project.id,
        revision=1,
        pack_json=_json_dump(pack),
        status="draft",
    )
    project.active_genesis_revision_id = row.id
    project.creation_status = "creating"
    session.add(project)
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="business_event",
            event_type=DecisionEventType.GENESIS_CREATED,
            actor_type="api",
            summary="Book Genesis 根层已初始化。",
            payload={"revision": 1},
            related_object_type="book_genesis_revision",
            related_object_id=row.id,
        )
    )
    session.flush()
    return row

def active_revision(self, session: Session, project: Project) -> BookGenesisRevision | None:
    return self.workspace.active_revision(session, project)
    revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
    if not revision_id:
        return None
    return session.get(BookGenesisRevision, revision_id)

def load_pack(self, revision) -> dict[str, Any]:
    return self.workspace.load_pack(revision)
    return _initial_pack_dummy_merge(_json_load_object(getattr(revision, "pack_json", "{}")))

def patch_pack(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
    patch: dict[str, Any],
    reason: str = "",
):
    return self.workspace.patch_pack(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
        patch=patch,
        reason=reason,
    )
    _ensure_revision_is_current(session, project, revision)
    current = self.load_pack(revision)
    previous_stage_payloads = {
        stage_key: _json_clone(_pack_stage_payload(current, stage_key))
        for stage_key in GENESIS_STAGE_ORDER
    }
    next_pack = _deep_merge(current, patch)
    if "world" in patch and isinstance(next_pack.get("world"), dict):
        next_pack["world"] = self._normalize_world_root_payload(
            project=project,
            payload=next_pack.get("world") or {},
            fallback=_fallback_world(project, current),
        )
    if "book_arc_blueprint" in patch and isinstance(next_pack.get("book_arc_blueprint"), dict):
        next_pack["book_arc_blueprint"] = self._normalize_blueprint_payload(
            project=project,
            payload=next_pack.get("book_arc_blueprint") or {},
            fallback=_fallback_blueprint(project, current),
        )
    now = _utc_iso()
    stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else {}
    for stage_key, section_key in _STAGE_TO_SECTION.items():
        patched = False
        if section_key in patch:
            if stage_key == "world":
                patched = not _deep_equal(
                    _world_stage_state_view(previous_stage_payloads.get(stage_key)),
                    _world_stage_state_view(_pack_stage_payload(next_pack, stage_key)),
                )
            else:
                patched = True
        elif "world" in patch and stage_key in {"world", "map", "story_engine"}:
            if stage_key == "world":
                patched = not _deep_equal(
                    _world_stage_state_view(previous_stage_payloads.get(stage_key)),
                    _world_stage_state_view(_pack_stage_payload(next_pack, stage_key)),
                )
            else:
                patched = not _deep_equal(previous_stage_payloads.get(stage_key), _pack_stage_payload(next_pack, stage_key))
        if not patched:
            continue
        state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
        state.update(
            {
                "stage_key": stage_key,
                "status": "edited",
                "locked": False,
                "updated_at": now,
            }
        )
        stage_states[stage_key] = state
    next_pack["stage_states"] = stage_states
    new_row = updater.create_book_genesis_revision(
        project_id=project.id,
        revision=int(getattr(revision, "revision", 0) or 0) + 1,
        pack_json=_json_dump(next_pack),
        based_on_revision_id=str(getattr(revision, "id", "") or ""),
        status="draft",
    )
    project.active_genesis_revision_id = new_row.id
    project.creation_status = "creating"
    session.add(project)
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="audit_action",
            event_type=DecisionEventType.GENESIS_UPDATED,
            actor_type="manual_ui",
            summary="Book Genesis 已更新。",
            reason=str(reason or ""),
            payload={"patched_sections": sorted(patch.keys())},
            related_object_type="book_genesis_revision",
            related_object_id=new_row.id,
        )
    )
    session.flush()
    return new_row

def generate_stage(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
    stage_key: str,
    event_type: str = DecisionEventType.GENESIS_STAGE_GENERATED,
):
    return self.workspace.generate_stage(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
        stage_key=stage_key,
        event_type=event_type,
    )
    if stage_key not in GENESIS_STAGE_ORDER:
        raise ValueError(f"未知 Genesis stage: {stage_key}")
    pack = self.load_pack(revision)
    generated, trace_payload = self._generate_stage_payload(project=project, pack=pack, stage_key=stage_key)
    _ensure_revision_is_current(session, project, revision)
    next_pack = dict(pack)
    _set_pack_stage_payload(next_pack, stage_key, generated)
    stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else _empty_stage_states()
    stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
    parent_trace_id = str(stage_state.get("last_trace_id", "") or "")
    stage_state.update(
        {
            "stage_key": stage_key,
            "status": "generated",
            "locked": False,
            "updated_at": _utc_iso(),
        }
    )
    stage_states[stage_key] = stage_state
    next_pack["stage_states"] = stage_states
    decision = updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="business_event",
            event_type=event_type,
            actor_type="api",
            summary=f"Genesis 阶段 {stage_key} 已生成。",
            payload={"stage_key": stage_key},
            related_object_type="book_genesis_revision",
            related_object_id=str(getattr(revision, "id", "") or ""),
        )
    )
    trace_payload = self._prepare_trace_payload_for_save(trace_payload, project_id=project.id)
    trace = updater.save_prompt_trace(
        project_id=project.id,
        genesis_revision_id=str(getattr(revision, "id", "") or ""),
        decision_event_id=decision.id,
        parent_trace_id=parent_trace_id,
        trace_scope="genesis",
        stage_key=stage_key,
        template_id=f"genesis:{stage_key}",
        template_version="v1",
        effective_system_prompt=str(trace_payload.get("effective_system_prompt", "")),
        prompt_layers_json=_json_dump(trace_payload.get("prompt_layers", [])),
        input_snapshot_json=_json_dump(trace_payload.get("input_snapshot", {})),
        model_profile_json=_json_dump(trace_payload.get("model_profile", {})),
        attempts_json=_json_dump(trace_payload.get("attempts", [])),
        output_summary_json=_json_dump(trace_payload.get("output_summary", {})),
        backend=str(trace_payload.get("backend", "") or ""),
        codex_job_id=str(trace_payload.get("codex_job_id", "") or ""),
        permission_profile=str(trace_payload.get("permission_profile", "") or ""),
        fallback_used=bool(trace_payload.get("fallback_used", False)),
    )
    self._record_llm_events_for_trace(
        updater=updater,
        project_id=project.id,
        trace_id=trace.id,
        trace_payload=trace_payload,
        decision_event_id=decision.id,
    )
    stage_state["last_trace_id"] = trace.id
    new_row = updater.create_book_genesis_revision(
        project_id=project.id,
        revision=int(getattr(revision, "revision", 0) or 0) + 1,
        pack_json=_json_dump(next_pack),
        based_on_revision_id=str(getattr(revision, "id", "") or ""),
        status="draft",
    )
    project.active_genesis_revision_id = new_row.id
    project.creation_status = "creating"
    session.add(project)
    session.flush()
    return new_row, trace

def refine_stage(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
    stage_key: str,
    instruction: str,
    target_path: str = "",
    reason: str = "",
):
    return self.workspace.refine_stage(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
        stage_key=stage_key,
        instruction=instruction,
        target_path=target_path,
        reason=reason,
    )
    normalized_instruction = str(instruction or "").strip()
    normalized_path = str(target_path or "").strip()
    if stage_key not in GENESIS_STAGE_ORDER:
        raise ValueError(f"未知 Genesis stage: {stage_key}")
    if not normalized_instruction:
        raise ValueError("refine instruction 不能为空")
    pack = self.load_pack(revision)
    refined_payload, trace_payload = self._refine_stage_payload(
        project=project,
        pack=pack,
        stage_key=stage_key,
        instruction=normalized_instruction,
        target_path=normalized_path,
    )
    _ensure_revision_is_current(session, project, revision)
    next_pack = dict(pack)
    _set_pack_stage_payload(next_pack, stage_key, refined_payload)
    stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else _empty_stage_states()
    stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
    parent_trace_id = str(stage_state.get("last_trace_id", "") or "")
    stage_state.update(
        {
            "stage_key": stage_key,
            "status": "edited",
            "locked": False,
            "updated_at": _utc_iso(),
        }
    )
    stage_states[stage_key] = stage_state
    next_pack["stage_states"] = stage_states
    decision = updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="audit_action",
            event_type=DecisionEventType.GENESIS_STAGE_REFINED,
            actor_type="manual_ui",
            summary=f"Genesis 阶段 {stage_key} 已按指令改写。",
            reason=str(reason or normalized_instruction),
            payload={"stage_key": stage_key, "instruction": normalized_instruction, "target_path": normalized_path},
            related_object_type="book_genesis_revision",
            related_object_id=str(getattr(revision, "id", "") or ""),
        )
    )
    trace_payload = self._prepare_trace_payload_for_save(trace_payload, project_id=project.id)
    trace = updater.save_prompt_trace(
        project_id=project.id,
        genesis_revision_id=str(getattr(revision, "id", "") or ""),
        decision_event_id=decision.id,
        parent_trace_id=parent_trace_id,
        trace_scope="genesis_refine",
        stage_key=stage_key,
        template_id=f"genesis_refine:{stage_key}",
        template_version="v1",
        effective_system_prompt=str(trace_payload.get("effective_system_prompt", "")),
        prompt_layers_json=_json_dump(trace_payload.get("prompt_layers", [])),
        input_snapshot_json=_json_dump(trace_payload.get("input_snapshot", {})),
        model_profile_json=_json_dump(trace_payload.get("model_profile", {})),
        attempts_json=_json_dump(trace_payload.get("attempts", [])),
        output_summary_json=_json_dump(trace_payload.get("output_summary", {})),
        backend=str(trace_payload.get("backend", "") or ""),
        codex_job_id=str(trace_payload.get("codex_job_id", "") or ""),
        permission_profile=str(trace_payload.get("permission_profile", "") or ""),
        fallback_used=bool(trace_payload.get("fallback_used", False)),
    )
    self._record_llm_events_for_trace(
        updater=updater,
        project_id=project.id,
        trace_id=trace.id,
        trace_payload=trace_payload,
        decision_event_id=decision.id,
    )
    stage_state["last_trace_id"] = trace.id
    new_row = updater.create_book_genesis_revision(
        project_id=project.id,
        revision=int(getattr(revision, "revision", 0) or 0) + 1,
        pack_json=_json_dump(next_pack),
        based_on_revision_id=str(getattr(revision, "id", "") or ""),
        status="draft",
    )
    project.active_genesis_revision_id = new_row.id
    project.creation_status = "creating"
    session.add(project)
    session.flush()
    return new_row, trace

def lock_stage(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
    stage_key: str,
):
    return self.workspace.lock_stage(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
        stage_key=stage_key,
    )
    if stage_key not in GENESIS_STAGE_ORDER:
        raise ValueError(f"未知 Genesis stage: {stage_key}")
    _ensure_revision_is_current(session, project, revision)
    pack = self.load_pack(revision)
    stage_states = pack.get("stage_states") if isinstance(pack.get("stage_states"), dict) else _empty_stage_states()
    stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
    stage_state.update(
        {
            "stage_key": stage_key,
            "status": "locked",
            "locked": True,
            "updated_at": _utc_iso(),
        }
    )
    stage_states[stage_key] = stage_state
    pack["stage_states"] = stage_states
    new_row = updater.create_book_genesis_revision(
        project_id=project.id,
        revision=int(getattr(revision, "revision", 0) or 0) + 1,
        pack_json=_json_dump(pack),
        based_on_revision_id=str(getattr(revision, "id", "") or ""),
        status="draft",
    )
    project.active_genesis_revision_id = new_row.id
    if _ready_for_start(pack):
        project.creation_status = "genesis_ready"
    session.add(project)
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="audit_action",
            event_type=DecisionEventType.GENESIS_STAGE_LOCKED,
            actor_type="manual_ui",
            summary=f"Genesis 阶段 {stage_key} 已锁定。",
            payload={"stage_key": stage_key},
            related_object_type="book_genesis_revision",
            related_object_id=new_row.id,
        )
    )
    session.flush()
    return new_row

def build_detail(self, *, session: Session, project: Project) -> dict[str, Any]:
    return self.workspace.build_detail(session=session, project=project)
    revision = self.active_revision(session, project)
    pack = self.load_pack(revision) if revision is not None else _initial_pack(project)
    prompt_traces = session.execute(
        select(PromptTrace)
        .where(PromptTrace.project_id == project.id)
        .order_by(PromptTrace.created_at.desc())
        .limit(50)
    ).scalars().all()
    return {
        "project_id": project.id,
        "creation_status": str(getattr(project, "creation_status", "") or "legacy"),
        "active_genesis_revision_id": str(getattr(project, "active_genesis_revision_id", "") or ""),
        "revision": int(getattr(revision, "revision", 1) or 1),
        "pack": pack,
        "prompt_traces": [
            {
                "id": row.id,
                "trace_scope": row.trace_scope,
                "stage_key": row.stage_key,
                "template_id": row.template_id,
                "template_version": row.template_version,
                "effective_system_prompt": row.effective_system_prompt,
                "prompt_layers": _json_load_object(row.prompt_layers_json).get("items", [])
                if False
                else _json_load_list_dicts(row.prompt_layers_json),
                "input_snapshot": _json_load_object(row.input_snapshot_json),
                "model_profile": _json_load_object(row.model_profile_json),
                "attempts": _json_load_list_dicts(row.attempts_json),
                "output_summary": _json_load_object(row.output_summary_json),
                "decision_event_id": row.decision_event_id,
                "parent_trace_id": row.parent_trace_id,
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
            for row in prompt_traces
        ],
        "can_start_writing": _ready_for_start(pack)
        and str(getattr(project, "creation_status", "") or "") == "genesis_ready",
    }

def generate_name_suggestions(
    self,
    *,
    project: Project,
    revision: BookGenesisRevision,
    stage_key: str,
    target_path: str,
    field_path: str,
    kind: str = "",
    count: int = 1,
    nonce: str = "",
    stage_payload_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return self.workspace.generate_name_suggestions(
        project=project,
        revision=revision,
        stage_key=stage_key,
        target_path=target_path,
        field_path=field_path,
        kind=kind,
        count=count,
        nonce=nonce,
        stage_payload_override=stage_payload_override,
    )
    normalized_stage = str(stage_key or "").strip()
    if normalized_stage not in GENESIS_STAGE_ORDER:
        raise ValueError("未知 Genesis stage。")
    pack = self.load_pack(revision)
    if isinstance(stage_payload_override, dict):
        pack = dict(pack)
        _set_pack_stage_payload(pack, normalized_stage, stage_payload_override)
    stage_payload = _pack_stage_payload(pack, normalized_stage)
    normalized_target = str(target_path or "").strip()
    normalized_field = str(field_path or "").strip()
    if not normalized_field:
        raise ValueError("field_path 不能为空。")
    resolved_kind = str(kind or "").strip() or _infer_name_kind(
        stage_key=normalized_stage,
        target_path=normalized_target,
        field_path=normalized_field,
    )
    if resolved_kind not in {"person", "region", "place", "epithet"}:
        raise ValueError("无法推断命名类型，请显式提供 kind。")
    culture_profile = self._resolve_name_generation_profile(
        stage_key=normalized_stage,
        pack=pack,
        stage_payload=stage_payload,
        target_path=normalized_target,
    )
    civilization = _culture_profile_generator_civilization(culture_profile)
    if not civilization:
        raise ValueError("当前对象没有可用的文化背景命名配置。")
    normalized_count = max(1, min(int(count or 1), 12))
    try:
        suggestions = _generate_culture_names(
            civilization=civilization,
            kind=resolved_kind,
            count=normalized_count,
            seed=":".join(
                [
                    str(project.id or ""),
                    str(getattr(revision, "id", "") or ""),
                    normalized_stage,
                    normalized_target,
                    normalized_field,
                    resolved_kind,
                    str(culture_profile.get("id", "") or ""),
                    str(nonce or ""),
                ]
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"名称生成失败：{exc}") from exc
    applied_value: Any = suggestions
    if not _field_expects_list(normalized_field) and normalized_count == 1:
        applied_value = suggestions[0]
    return {
        "ok": True,
        "stage_key": normalized_stage,
        "target_path": normalized_target,
        "field_path": normalized_field,
        "kind": resolved_kind,
        "suggestions": suggestions,
        "applied_value": applied_value,
        "culture_profile_id": str(culture_profile.get("id", "")).strip(),
        "culture_profile_name": str(culture_profile.get("name", "")).strip(),
        "generator_civilization": civilization,
        "message": "已根据文化背景生成名称建议。",
    }

def _resolve_name_generation_profile(
    self,
    *,
    stage_key: str,
    pack: dict[str, Any],
    stage_payload: dict[str, Any],
    target_path: str,
) -> dict[str, Any]:
    normalized_target_path = _normalize_stage_target_path(stage_key, target_path)
    world_root = _pack_stage_payload(pack, "world")
    world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
    profiles = [
        item
        for item in (world_bible.get("culture_profiles") or [])
        if isinstance(item, dict)
    ]
    profile_by_id = {
        str(item.get("id", "")).strip(): item
        for item in profiles
        if str(item.get("id", "")).strip()
    }
    if stage_key == "world" and normalized_target_path.startswith("world_bible.culture_profiles["):
        target = _get_value_at_path(stage_payload, normalized_target_path)
        if isinstance(target, dict):
            return target
    target_value = None
    if normalized_target_path:
        try:
            target_value = _get_value_at_path(stage_payload, normalized_target_path)
        except ValueError:
            target_value = None
    if isinstance(target_value, dict):
        culture_profile_id = str(target_value.get("culture_profile_id", "")).strip()
        if culture_profile_id and culture_profile_id in profile_by_id:
            return profile_by_id[culture_profile_id]
    if profiles:
        return profiles[0]
    return _fallback_culture_profiles()[0]



__all__ = ['create_initial_revision', 'active_revision', 'load_pack', 'patch_pack', 'generate_stage', 'refine_stage', 'lock_stage', 'build_detail', 'generate_name_suggestions', '_resolve_name_generation_profile']
