from __future__ import annotations

from forwin.orchestrator_loop_core.common import *

@staticmethod
def _prompt_trace_success_summary(writer_output: WriterOutput) -> dict[str, object]:
    generation_meta = getattr(writer_output, "generation_meta", {}) or {}
    prompt_trace = generation_meta.get("prompt_trace") if isinstance(generation_meta, dict) else {}
    attempts = prompt_trace.get("attempts", []) if isinstance(prompt_trace, dict) else []
    if not isinstance(attempts, list):
        attempts = []
    successful = None
    for item in attempts:
        if not isinstance(item, dict):
            continue
        if int(item.get("output_chars") or 0) > 0 and not str(item.get("error_class") or ""):
            successful = item
    if successful is None and attempts:
        successful = next((item for item in reversed(attempts) if isinstance(item, dict)), None)
    if not isinstance(successful, dict):
        return {
            "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
            "effective_model": "",
            "effective_profile_id": "",
            "successful_attempt_no": 0,
            "attempt_group_id": "",
            "output_chars": int(getattr(writer_output, "char_count", 0) or 0),
            "fallback_chain": generation_meta.get("model_fallbacks", []),
        }
    return {
        "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
        "effective_model": str(successful.get("model") or ""),
        "effective_profile_id": str(successful.get("profile_id") or ""),
        "effective_profile_name": str(successful.get("profile_name") or ""),
        "successful_attempt_no": int(successful.get("attempt_no") or 0),
        "attempt_group_id": str(successful.get("attempt_group_id") or ""),
        "output_chars": int(successful.get("output_chars") or getattr(writer_output, "char_count", 0) or 0),
        "fallback_chain": generation_meta.get("model_fallbacks", []),
    }

def _apply_world_v4_gate(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    verdict: ReviewVerdict,
) -> str | None:
    chapter_intent = WorldContractRepository(session).get_chapter_intent(
        project_id,
        chapter_number,
    )
    writer_output_for_direct = writer_output.model_copy(update={"project_id": project_id})
    broker = RetrievalBroker()
    writer_pack = broker.build_world_model_pack(
        repo,
        project_id,
        chapter_number,
        "writing",
    )
    review_pack = broker.build_world_model_pack(
        repo,
        project_id,
        chapter_number,
        "review",
    )
    compiler_pack = broker.build_world_model_pack(
        repo,
        project_id,
        chapter_number,
        "compiler",
    )
    retrieval_pack_payload = {
        "writing": writer_pack.model_dump(mode="json"),
        "review": review_pack.model_dump(mode="json"),
        "compiler": compiler_pack.model_dump(mode="json"),
    }
    default_book_state_layers = ["world", "map", "cognition", "narrative"]
    extractor = BookStateGraphDeltaExtractor(
        layers=set(
            getattr(self.config, "book_state_layers", default_book_state_layers)
            or default_book_state_layers
        )
    )
    extraction = extractor.extract(
        BookStateExtractionRequest(
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output_for_direct,
            chapter_intent=chapter_intent,
            review_verdict_id=f"book_state_direct_review_{project_id}_{chapter_number}",
        )
    )
    gate_verdict = extraction.compatibility_gate_verdict
    if not extraction.accepted or extraction.changes is None:
        frozen_path = ""
        if self.config.freeze_failed_candidates:
            frozen_path = self.artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "book-state-direct-extraction-blocked",
                    "chapter_number": chapter_number,
                    "writer_output": writer_output.model_dump(mode="json"),
                    "review_verdict": verdict.model_dump(mode="json"),
                    "book_state_extraction": extraction.model_dump(mode="json"),
                    "v4_retrieval_packs": retrieval_pack_payload,
                },
            )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState direct extraction 阻止 canon 写入。",
            payload={
                "book_state_extraction_issues": [
                    issue.model_dump(mode="json") for issue in extraction.issues
                ],
                "extraction_path": "book_state_direct",
            },
        )
        return frozen_path or "book-state-direct-extraction-blocked"

    book_state_changes = extraction.changes
    if not book_state_changes.graph_deltas:
        return None
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.BOOK_STATE_REVIEW_STARTED,
        scope="chapter",
        summary=f"第{chapter_number}章 BookState review gate 开始。",
        payload=audit_payload(
            stage="book_state_review",
            status="started",
            operation_id=self._audit_operation_id(),
            graph_delta_count=len(book_state_changes.graph_deltas),
            extraction_path="book_state_direct",
        ),
    )
    commit_service = BookStateDirectCommitService(session)
    book_state_verdict = commit_service.review(book_state_changes)
    record_compat = getattr(self, "_record_legacy_compatibility_event", None)
    if callable(record_compat):
        for issue in book_state_verdict.issues:
            compat_payload = getattr(issue, "legacy_compatibility", {}) or {}
            if not isinstance(compat_payload, dict) or not compat_payload:
                continue
            record_compat(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                compat_layer=str(compat_payload.get("compat_layer") or "book_state"),
                compat_feature=str(compat_payload.get("compat_feature") or ""),
                usage_kind=str(compat_payload.get("usage_kind") or "read_fallback"),
                source_module=str(compat_payload.get("source_module") or "forwin.book_state.reviewer"),
                usage_reason=str(compat_payload.get("usage_reason") or issue.message),
                compat_key=str(compat_payload.get("compat_key") or ""),
                legacy_identifier=str(compat_payload.get("legacy_identifier") or ""),
                canonical_identifier=str(compat_payload.get("canonical_identifier") or ""),
                related_stage=str(compat_payload.get("related_stage") or "book_state_review"),
            )
    if not book_state_verdict.accepted or book_state_verdict.approved_changes is None:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.BOOK_STATE_REVIEW_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState review gate 未通过。",
            payload=audit_payload(
                stage="book_state_review",
                status="failed",
                operation_id=self._audit_operation_id(),
                issue_count=len(book_state_verdict.issues),
                issues=[issue.model_dump(mode="json") for issue in book_state_verdict.issues],
                extraction_path="book_state_direct",
            ),
        )
        frozen_path = ""
        if self.config.freeze_failed_candidates:
            frozen_path = self.artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "book-state-review-gate-blocked",
                    "chapter_number": chapter_number,
                    "writer_output": writer_output.model_dump(mode="json"),
                    "book_state_review": book_state_verdict.model_dump(mode="json"),
                    "book_state_extraction": extraction.model_dump(mode="json"),
                },
            )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState review gate 阻止 canon 写入。",
            payload={
                "book_state_review_issues": [
                    issue.model_dump(mode="json") for issue in book_state_verdict.issues
                ],
            },
        )
        return frozen_path or "book-state-review-gate-blocked"

    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.BOOK_STATE_REVIEW_SUCCEEDED,
        scope="chapter",
        summary=f"第{chapter_number}章 BookState review gate 通过。",
        payload=audit_payload(
            stage="book_state_review",
            status="succeeded",
            operation_id=self._audit_operation_id(),
            issue_count=len(book_state_verdict.issues),
            extraction_path="book_state_direct",
        ),
    )

    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.BOOK_STATE_COMPILE_STARTED,
        scope="chapter",
        summary=f"第{chapter_number}章 BookState compile 开始。",
        payload=audit_payload(
            stage="book_state_compile",
            status="started",
            operation_id=self._audit_operation_id(),
            graph_delta_count=len(book_state_verdict.approved_changes.graph_deltas),
            extraction_path="book_state_direct",
        ),
    )
    try:
        book_state_result = commit_service.compile_approved(
            book_state_verdict.approved_changes,
            compiler_run_id=f"book_state_compile_{project_id}_{chapter_number}",
        )
    except Exception as exc:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.BOOK_STATE_COMPILE_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState compile 异常失败。",
            reason=str(exc),
            payload=event_error_payload(
                exc,
                stage="book_state_compile",
                operation_id=self._audit_operation_id(),
            ),
        )
        raise
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=(
            DecisionEventType.BOOK_STATE_COMPILE_SUCCEEDED
            if book_state_result.committed
            else DecisionEventType.BOOK_STATE_COMPILE_FAILED
        ),
        scope="chapter",
        summary=(
            f"第{chapter_number}章 BookState compile 完成。"
            if book_state_result.committed
            else f"第{chapter_number}章 BookState compile 未提交。"
        ),
        payload=audit_payload(
            stage="book_state_compile",
            status="succeeded" if book_state_result.committed else "failed",
            operation_id=self._audit_operation_id(),
            result=book_state_result.model_dump(mode="json"),
            extraction_path="book_state_direct",
        ),
    )
    if not book_state_result.committed:
        frozen_path = ""
        if self.config.freeze_failed_candidates:
            frozen_path = self.artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "book-state-compile-blocked",
                    "chapter_number": chapter_number,
                    "writer_output": writer_output.model_dump(mode="json"),
                    "book_state_review": book_state_verdict.model_dump(mode="json"),
                    "book_state_result": book_state_result.model_dump(mode="json"),
                    "book_state_extraction": extraction.model_dump(mode="json"),
                },
            )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState compile 阻止 canon 写入。",
            payload={"book_state_blocked_reasons": list(book_state_result.blocked_reasons)},
        )
        return frozen_path or "book-state-compile-blocked"

    if self.config.world_v4_compat_write_enabled and gate_verdict is not None:
        record_compat = getattr(self, "_record_legacy_compatibility_event", None)
        if callable(record_compat):
            record_compat(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                compat_layer="projection",
                compat_feature="projection.legacy_world_model_projection",
                usage_kind="projection_compat",
                source_module="forwin.orchestrator_loop_core.world_projection",
                usage_reason="world_v4 compatibility projection path invoked",
                related_stage="legacy_projection",
            )
        legacy_nested = session.begin_nested()
        try:
            compiler_result = WorldModelCompilerV4(session).compile_gate_verdict(
                project_id=project_id,
                chapter_number=chapter_number,
                verdict=gate_verdict,
                compiler_run_id=f"compile_{project_id}_{chapter_number}",
                retrieval_pack_payload=retrieval_pack_payload,
            )
            if compiler_result.committed:
                legacy_nested.commit()
            else:
                legacy_nested.rollback()
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LEGACY_PROJECTION_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 world_model_v4 compatibility projection 未提交，BookState canon 已保留。",
                    payload=audit_payload(
                        stage="legacy_projection",
                        status="failed",
                        operation_id=self._audit_operation_id(),
                        result=compiler_result.model_dump(mode="json"),
                    ),
                )
        except Exception as exc:
            legacy_nested.rollback()
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.LEGACY_PROJECTION_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 world_model_v4 compatibility projection 失败，BookState canon 已保留。",
                reason=str(exc),
                payload=event_error_payload(
                    exc,
                    stage="legacy_projection",
                    operation_id=self._audit_operation_id(),
                ),
            )

    projection_refresh = KnowledgeProjectionRefresher(
        session,
        qdrant_url=self.config.qdrant_url,
        qdrant_collection=self.config.llm_kb_qdrant_collection,
    ).refresh(
        project_id,
        as_of_chapter=chapter_number,
        trigger="chapter_accepted",
    )
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.KNOWLEDGE_PROJECTION_REFRESHED,
        scope="chapter",
        summary=f"第{chapter_number}章 BookState projection refresh 完成。",
        payload=projection_refresh.as_dict(),
    )
    return None

@staticmethod
def _filter_resolvable_events(
    repo: StateRepository,
    project_id: str,
    chapter_number: int,
    events: list[EventCandidate],
) -> list[EventCandidate]:
    entity_lookup = repo.get_entities_by_names(
        project_id,
        [
            name
            for event in events
            for name in event.involved_entity_names
        ],
    )
    filtered: list[EventCandidate] = []
    for event in events:
        unknown_names = [
            name
            for name in event.involved_entity_names
            if entity_lookup.get(name) is None
        ]
        if unknown_names:
            logger.warning(
                "Dropping event %r in chapter %d because entities are unknown: %s",
                event.summary,
                chapter_number,
                ", ".join(unknown_names),
            )
            continue
        filtered.append(event)
    return filtered

@staticmethod
def _filter_resolvable_state_changes(
    repo: StateRepository,
    project_id: str,
    chapter_number: int,
    changes: list,
) -> list:
    character_names = [
        str(change.entity_name or "").strip()
        for change in changes
        if str(getattr(change, "entity_kind", "") or "") == "character"
        and str(change.entity_name or "").strip()
    ]
    entity_lookup = repo.get_entities_by_names(project_id, character_names)
    filtered: list = []
    for change in changes:
        entity_name = str(change.entity_name or "").strip()
        if str(getattr(change, "entity_kind", "") or "") == "character" and entity_name:
            if entity_lookup.get(entity_name) is None:
                logger.warning(
                    "Dropping state change for unknown character %r in chapter %d.",
                    entity_name,
                    chapter_number,
                )
                continue
        filtered.append(change)
    return filtered

@staticmethod
def _ensure_genesis_canon_seed_entities(
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    project_id: str,
) -> None:
    project = session.get(Project, project_id)
    if project is None:
        return
    revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
    revision = session.get(BookGenesisRevision, revision_id) if revision_id else None
    if revision is None:
        return
    try:
        pack = json.loads(str(getattr(revision, "pack_json", "") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(pack, dict):
        return
    world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
    story_engine = world.get("story_engine") if isinstance(world.get("story_engine"), dict) else {}
    seed_specs: list[tuple[str, str, dict]] = []
    for collection_key, entity_kind in (
        ("core_cast", "character"),
        ("characters", "character"),
        ("factions", "faction"),
        ("opposition", "character"),
    ):
        for item in story_engine.get(collection_key) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or "").strip()
            if not name or len(name) > 40:
                continue
            seed_specs.append((entity_kind, name, item))
    for anchor in ContinuityChecker(repo)._canon_name_anchors(project_id):
        name = str(getattr(anchor, "canonical_name", "") or "").strip()
        role_label = str(getattr(anchor, "role_label", "") or "").strip()
        if not name or len(name) > 40:
            continue
        seed_specs.append(
            (
                "character",
                name,
                {
                    "role": f"{role_label} canon name anchor" if role_label else "canon name anchor",
                    "aliases": [role_label] if role_label else [],
                },
            )
        )
    if not seed_specs:
        return

    changed = False
    seen: set[tuple[str, str]] = set()
    for entity_kind, name, payload in seed_specs:
        key = (entity_kind, name)
        if key in seen:
            continue
        seen.add(key)
        existing = repo.get_entities_by_names(project_id, [name]).get(name)
        if existing is not None:
            continue
        aliases = [
            str(alias).strip()
            for alias in (payload.get("aliases") or [])
            if str(alias).strip()
        ]
        if "/" in name:
            aliases.extend(part.strip() for part in name.split("/") if part.strip() and part.strip() != name)
        description_parts = [
            str(payload.get(key_name) or "").strip()
            for key_name in ("role", "desire", "fear", "secret", "goal", "leverage")
            if str(payload.get(key_name) or "").strip()
        ]
        updater.create_entity(
            project_id=project_id,
            kind=entity_kind,
            name=name,
            description="；".join(description_parts),
            aliases=list(dict.fromkeys(aliases)),
            importance=8 if entity_kind == "character" else 7,
            chapter=0,
        )
        changed = True
    if changed:
        SubWorldManager().ensure_registry(session, project_id)

@staticmethod
def _collect_subworld_candidate_names(
    repo: StateRepository,
    project_id: str,
    writer_output: WriterOutput,
) -> set[str]:
    names: set[str] = set()
    maybe_event_names: set[str] = set()
    maybe_state_change_names: set[str] = set()
    absence_only_names = {
        name
        for change in writer_output.state_changes
        if change.entity_kind == "character"
        and ContinuityChecker._is_absence_only_state_change(change)
        for name in [ContinuityChecker._candidate_character_name(change.entity_name)]
        if name
    }
    for mention in getattr(writer_output, "entity_mentions", []):
        if (
            getattr(mention, "entity_kind", "") == "character"
            and bool(getattr(mention, "is_named", False))
            and bool(getattr(mention, "is_on_stage", True))
        ):
            entity_name = ContinuityChecker._candidate_character_name(
                getattr(mention, "entity_name", "")
            )
            if entity_name and entity_name not in absence_only_names:
                names.add(entity_name)
    for change in writer_output.state_changes:
        if (
            change.entity_kind == "character"
            and not ContinuityChecker._is_absence_only_state_change(change)
        ):
            entity_name = ContinuityChecker._candidate_character_name(change.entity_name)
            if not entity_name:
                continue
            maybe_state_change_names.add(entity_name)
    for event in writer_output.new_events:
        for entity_name in event.involved_entity_names:
            normalized = ContinuityChecker._candidate_character_name(entity_name)
            if normalized and normalized not in absence_only_names:
                maybe_event_names.add(normalized)
    for scene in writer_output.scene_outputs:
        for entity_name in scene.involved_entities:
            normalized = ContinuityChecker._candidate_character_name(entity_name)
            if normalized and normalized not in absence_only_names:
                names.add(normalized)
    if maybe_event_names:
        resolved = repo.get_entities_by_names(project_id, sorted(maybe_event_names))
        for entity_name in maybe_event_names:
            entity = resolved.get(entity_name)
            if entity is not None and entity.kind == "character":
                names.add(entity_name)
    if maybe_state_change_names:
        resolved = repo.get_entities_by_names(project_id, sorted(maybe_state_change_names))
        for entity_name in maybe_state_change_names:
            entity = resolved.get(entity_name)
            if entity is not None and entity.kind == "character":
                names.add(entity_name)
    return {name for name in names if len(name) <= 12}

def _validate_subworld_admission(
    self,
    *,
    repo: StateRepository,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
) -> None:
    allowed_names = {
        ContinuityChecker._normalize_character_reference(name)
        for name in repo.get_allowed_entity_names(project_id, chapter_number)
    }
    allowed_names.update(
        ContinuityChecker._normalize_character_reference(anchor.canonical_name)
        for anchor in ContinuityChecker(repo)._canon_name_anchors(project_id)
    )
    allowed_names.update(
        ContinuityChecker._normalize_character_reference(name)
        for name in self._project_character_names(repo, project_id)
    )
    if not allowed_names:
        return
    unknown = sorted(
        name
        for name in self._collect_subworld_candidate_names(repo, project_id, writer_output)
        if name not in allowed_names
    )
    if unknown:
        raise ValueError(
            "Subworld admission rejected chapter "
            f"{chapter_number}: {', '.join(unknown)}"
        )

def _run_phase3_pass(
    self,
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> None:
    stage = self.stage_analyzer.analyze(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    pacing = self.pacing_strategist.analyze(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    save_stage_analysis(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage=stage,
        pacing=pacing,
    )
    self.replan_governor.apply_if_needed(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage=stage,
        pacing=pacing,
    )
    self.arc_envelope_manager.ensure_active_arc_resolution(
        session=session,
        project_id=project_id,
        activation_chapter=chapter_number + 1,
    )
    self.arc_envelope_manager.record_provisional_promotion(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        reason="accepted-into-canon",
    )
    intents = self.npc_intent_generator.generate(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    self._flush_background_llm_trace(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage_key="npc_intents",
        trace_scope="phase4",
    )
    save_npc_intents(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        intents=intents,
    )
    world_turn = self.world_simulator.simulate(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    self._flush_background_llm_trace(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage_key="world_pressure",
        trace_scope="phase4",
    )
    save_world_turn(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        turn=world_turn,
    )
    # Phase B: windowed signal aggregation + cooldown filter
    run_feedback_aggregation_pass(
        session,
        project_id,
        chapter_number,
        cooldown_chapters=self.config.feedback_cooldown_chapters,
        comment_to_reader_ratio=self.config.comment_to_reader_ratio,
    )



__all__ = ['_prompt_trace_success_summary', '_apply_world_v4_gate', '_filter_resolvable_events', '_filter_resolvable_state_changes', '_ensure_genesis_canon_seed_entities', '_collect_subworld_candidate_names', '_validate_subworld_admission', '_run_phase3_pass']
