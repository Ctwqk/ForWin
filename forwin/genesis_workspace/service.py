from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.project import Project
from forwin.state.updater import StateUpdater

from .name_suggestions import GenesisNameSuggestionService
from .normalizer import GenesisNormalizer
from .prompts import GenesisPromptBuilder
from .revision_service import GenesisRevisionService


def _legacy():
    from forwin import book_genesis as legacy

    return legacy


class GenesisWorkspaceService:
    """Human-facing Genesis workspace operations.

    This service owns revision edits, stage generation/refinement/locking,
    detail reads, and name suggestions. It deliberately does not materialize
    writing runtime rows or create generation tasks.
    """

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.revisions = GenesisRevisionService()
        self.prompts = GenesisPromptBuilder(owner)
        self.normalizer = GenesisNormalizer(owner)
        self.name_suggestions = GenesisNameSuggestionService(owner)

    def active_revision(self, session: Session, project: Project) -> BookGenesisRevision | None:
        return self.revisions.active_revision(session, project)

    def load_pack(self, revision: BookGenesisRevision | None) -> dict[str, Any]:
        return self.revisions.load_pack(revision)

    def create_initial_revision(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        brief_seed: dict[str, Any] | None = None,
    ) -> BookGenesisRevision:
        legacy = _legacy()
        pack = self.revisions.create_initial_pack(project, brief_seed)
        row = updater.create_book_genesis_revision(
            project_id=project.id,
            revision=1,
            pack_json=legacy._json_dump(pack),
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

    def patch_pack(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        patch: dict[str, Any],
        reason: str = "",
    ) -> BookGenesisRevision:
        legacy = _legacy()
        legacy._ensure_revision_is_current(session, project, revision)
        current = self.load_pack(revision)
        previous_stage_payloads = {
            stage_key: legacy._json_clone(legacy._pack_stage_payload(current, stage_key))
            for stage_key in legacy.GENESIS_STAGE_ORDER
        }
        next_pack = legacy._deep_merge(current, patch)
        if "world" in patch and isinstance(next_pack.get("world"), dict):
            next_pack["world"] = self.normalizer.normalize_world_root(
                project=project,
                payload=next_pack.get("world") or {},
                fallback=legacy._fallback_world(project, current),
            )
        if "book_arc_blueprint" in patch and isinstance(next_pack.get("book_arc_blueprint"), dict):
            next_pack["book_arc_blueprint"] = self.normalizer.normalize_book_blueprint(
                project=project,
                payload=next_pack.get("book_arc_blueprint") or {},
                fallback=legacy._fallback_blueprint(project, current),
            )
        now = legacy._utc_iso()
        stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else {}
        for stage_key, section_key in legacy._STAGE_TO_SECTION.items():
            patched = False
            if section_key in patch:
                if stage_key == "world":
                    patched = not legacy._deep_equal(
                        legacy._world_stage_state_view(previous_stage_payloads.get(stage_key)),
                        legacy._world_stage_state_view(legacy._pack_stage_payload(next_pack, stage_key)),
                    )
                else:
                    patched = True
            elif "world" in patch and stage_key in {"world", "map", "story_engine"}:
                if stage_key == "world":
                    patched = not legacy._deep_equal(
                        legacy._world_stage_state_view(previous_stage_payloads.get(stage_key)),
                        legacy._world_stage_state_view(legacy._pack_stage_payload(next_pack, stage_key)),
                    )
                else:
                    patched = not legacy._deep_equal(
                        previous_stage_payloads.get(stage_key),
                        legacy._pack_stage_payload(next_pack, stage_key),
                    )
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
            pack_json=legacy._json_dump(next_pack),
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
        revision: BookGenesisRevision,
        stage_key: str,
        event_type: str = DecisionEventType.GENESIS_STAGE_GENERATED,
    ) -> tuple[BookGenesisRevision, PromptTrace]:
        legacy = _legacy()
        if stage_key not in legacy.GENESIS_STAGE_ORDER:
            raise ValueError(f"未知 Genesis stage: {stage_key}")
        pack = self.load_pack(revision)
        generated, trace_payload = self.owner._generate_stage_payload(project=project, pack=pack, stage_key=stage_key)
        legacy._ensure_revision_is_current(session, project, revision)
        next_pack = dict(pack)
        legacy._set_pack_stage_payload(next_pack, stage_key, generated)
        stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else legacy._empty_stage_states()
        stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
        parent_trace_id = str(stage_state.get("last_trace_id", "") or "")
        stage_state.update(
            {
                "stage_key": stage_key,
                "status": "generated",
                "locked": False,
                "updated_at": legacy._utc_iso(),
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
        trace_payload = self.owner._prepare_trace_payload_for_save(trace_payload, project_id=project.id)
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
            prompt_layers_json=legacy._json_dump(trace_payload.get("prompt_layers", [])),
            input_snapshot_json=legacy._json_dump(trace_payload.get("input_snapshot", {})),
            model_profile_json=legacy._json_dump(trace_payload.get("model_profile", {})),
            attempts_json=legacy._json_dump(trace_payload.get("attempts", [])),
            output_summary_json=legacy._json_dump(trace_payload.get("output_summary", {})),
            backend=str(trace_payload.get("backend", "") or ""),
            codex_job_id=str(trace_payload.get("codex_job_id", "") or ""),
            permission_profile=str(trace_payload.get("permission_profile", "") or ""),
            fallback_used=bool(trace_payload.get("fallback_used", False)),
        )
        self.owner._record_llm_events_for_trace(
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
            pack_json=legacy._json_dump(next_pack),
            based_on_revision_id=str(getattr(revision, "id", "") or ""),
            status="draft",
        )
        project.active_genesis_revision_id = new_row.id
        project.creation_status = "creating"
        session.add(project)
        session.flush()
        return new_row, trace

    def rerun_stage(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        stage_key: str,
    ) -> tuple[BookGenesisRevision, PromptTrace]:
        return self.generate_stage(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            stage_key=stage_key,
            event_type=DecisionEventType.GENESIS_STAGE_RERUN,
        )

    def refine_stage(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        stage_key: str,
        instruction: str,
        target_path: str = "",
        reason: str = "",
    ) -> tuple[BookGenesisRevision, PromptTrace]:
        legacy = _legacy()
        normalized_instruction = str(instruction or "").strip()
        normalized_path = str(target_path or "").strip()
        if stage_key not in legacy.GENESIS_STAGE_ORDER:
            raise ValueError(f"未知 Genesis stage: {stage_key}")
        if not normalized_instruction:
            raise ValueError("refine instruction 不能为空")
        pack = self.load_pack(revision)
        refined_payload, trace_payload = self.owner._refine_stage_payload(
            project=project,
            pack=pack,
            stage_key=stage_key,
            instruction=normalized_instruction,
            target_path=normalized_path,
        )
        legacy._ensure_revision_is_current(session, project, revision)
        next_pack = dict(pack)
        legacy._set_pack_stage_payload(next_pack, stage_key, refined_payload)
        stage_states = next_pack.get("stage_states") if isinstance(next_pack.get("stage_states"), dict) else legacy._empty_stage_states()
        stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
        parent_trace_id = str(stage_state.get("last_trace_id", "") or "")
        stage_state.update(
            {
                "stage_key": stage_key,
                "status": "edited",
                "locked": False,
                "updated_at": legacy._utc_iso(),
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
        trace_payload = self.owner._prepare_trace_payload_for_save(trace_payload, project_id=project.id)
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
            prompt_layers_json=legacy._json_dump(trace_payload.get("prompt_layers", [])),
            input_snapshot_json=legacy._json_dump(trace_payload.get("input_snapshot", {})),
            model_profile_json=legacy._json_dump(trace_payload.get("model_profile", {})),
            attempts_json=legacy._json_dump(trace_payload.get("attempts", [])),
            output_summary_json=legacy._json_dump(trace_payload.get("output_summary", {})),
            backend=str(trace_payload.get("backend", "") or ""),
            codex_job_id=str(trace_payload.get("codex_job_id", "") or ""),
            permission_profile=str(trace_payload.get("permission_profile", "") or ""),
            fallback_used=bool(trace_payload.get("fallback_used", False)),
        )
        self.owner._record_llm_events_for_trace(
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
            pack_json=legacy._json_dump(next_pack),
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
        revision: BookGenesisRevision,
        stage_key: str,
    ) -> BookGenesisRevision:
        legacy = _legacy()
        if stage_key not in legacy.GENESIS_STAGE_ORDER:
            raise ValueError(f"未知 Genesis stage: {stage_key}")
        legacy._ensure_revision_is_current(session, project, revision)
        pack = self.load_pack(revision)
        stage_states = pack.get("stage_states") if isinstance(pack.get("stage_states"), dict) else legacy._empty_stage_states()
        stage_state = stage_states.get(stage_key) if isinstance(stage_states.get(stage_key), dict) else {}
        stage_state.update(
            {
                "stage_key": stage_key,
                "status": "locked",
                "locked": True,
                "updated_at": legacy._utc_iso(),
            }
        )
        stage_states[stage_key] = stage_state
        pack["stage_states"] = stage_states
        new_row = updater.create_book_genesis_revision(
            project_id=project.id,
            revision=int(getattr(revision, "revision", 0) or 0) + 1,
            pack_json=legacy._json_dump(pack),
            based_on_revision_id=str(getattr(revision, "id", "") or ""),
            status="draft",
        )
        project.active_genesis_revision_id = new_row.id
        if legacy._ready_for_start(pack):
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
        legacy = _legacy()
        revision = self.active_revision(session, project)
        pack = self.load_pack(revision) if revision is not None else legacy._initial_pack(project)
        prompt_traces = session.execute(
            select(PromptTrace)
            .where(PromptTrace.project_id == project.id)
            .order_by(PromptTrace.created_at.desc())
            .limit(50)
        ).scalars().all()
        return {
            "project_id": project.id,
            "creation_status": str(getattr(project, "creation_status", "") or "creating"),
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
                    "prompt_layers": legacy._json_load_list_dicts(row.prompt_layers_json),
                    "input_snapshot": legacy._json_load_object(row.input_snapshot_json),
                    "model_profile": legacy._json_load_object(row.model_profile_json),
                    "attempts": legacy._json_load_list_dicts(row.attempts_json),
                    "output_summary": legacy._json_load_object(row.output_summary_json),
                    "decision_event_id": row.decision_event_id,
                    "parent_trace_id": row.parent_trace_id,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
                for row in prompt_traces
            ],
            "can_start_writing": legacy._ready_for_start(pack)
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
        return self.name_suggestions.generate_name_suggestions(
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
