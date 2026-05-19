from __future__ import annotations

from forwin.orchestrator_loop_core.common import *

def _flush_background_llm_trace(
    self,
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    stage_key: str,
    trace_scope: str,
) -> str:
    drain_attempts = getattr(self.llm_client, "drain_llm_attempt_events", None)
    attempts = drain_attempts() if callable(drain_attempts) else []
    if not attempts:
        return ""
    return self._save_prompt_trace_payload(
        session=session,
        updater=StateUpdater(session),
        project_id=project_id,
        prompt_trace={
            "trace_scope": trace_scope,
            "stage_key": stage_key,
            "template_id": f"{trace_scope}:{stage_key}",
            "template_version": "v1",
            "effective_system_prompt": "",
            "prompt_layers": [],
            "input_snapshot": {
                "project_id": project_id,
                "chapter_number": chapter_number,
                "stage_key": stage_key,
            },
            "model_profile": {
                "profile_id": getattr(self.llm_client, "profile_id", ""),
                "profile_name": getattr(self.llm_client, "profile_name", ""),
                "model": getattr(self.llm_client, "model", ""),
                "base_url": getattr(self.llm_client, "base_url", ""),
            },
            "attempts": attempts,
            "output_summary": {
                "status": "recorded",
                "chapter_number": chapter_number,
            },
        },
    )

def _compile_world_model_after_acceptance(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
) -> bool:
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.WORLD_MODEL_COMPILE_STARTED,
        scope="chapter",
        summary=f"第{chapter_number}章 WorldModel compile 开始。",
    )
    record_compat = getattr(self, "_record_legacy_compatibility_event", None)
    if callable(record_compat):
        record_compat(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            compat_layer="projection",
            compat_feature="projection.legacy_world_model_projection",
            usage_kind="projection_compat",
            source_module="forwin.orchestrator_loop_core.finalization",
            usage_reason="legacy WorldModel projection compile path invoked",
            related_stage="world_model_compile",
        )
    try:
        snapshot = LegacyWorldModelCompiler(session).compile_after_chapter(project_id, chapter_number)
    except Exception as exc:
        logger.exception("WorldModel compile failed for chapter %d.", chapter_number)
        try:
            LegacyWorldModelCompiler(session).record_failed_compile(
                project_id=project_id,
                as_of_chapter=chapter_number,
                trigger="chapter_accepted",
                error=f"{exc.__class__.__name__}: {exc}",
            )
        except Exception:
            logger.warning("Failed to record WorldModel failed compile run.", exc_info=True)
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.WORLD_MODEL_COMPILE_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 legacy WorldModel projection 失败，BookState canon 已保留。",
            reason=str(exc),
            payload=event_error_payload(
                exc,
                stage="world_model_compile",
                operation_id=self._audit_operation_id(),
            ),
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.LEGACY_PROJECTION_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 legacy world_model_v4 projection 失败，BookState canon 不回滚。",
            reason=str(exc),
            payload=event_error_payload(
                exc,
                stage="legacy_projection",
                operation_id=self._audit_operation_id(),
            ),
        )
        return True
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.WORLD_MODEL_COMPILE_SUCCEEDED,
        scope="chapter",
        summary=f"第{chapter_number}章 WorldModel compile 完成。",
        related_object_type="world_model_snapshot",
        related_object_id=snapshot.id,
        payload=audit_payload(
            stage="world_model_compile",
            status="succeeded",
            operation_id=self._audit_operation_id(),
            snapshot_id=snapshot.id,
            as_of_chapter=snapshot.as_of_chapter,
            source_digest=snapshot.source_digest,
        ),
    )
    return True

def _run_provisional_band_preview(
    self,
    *,
    session: Session,
    project_id: str,
    arc_id: str,
    band_id: str,
    chapter_plans: list[ChapterPlan],
    persist_result: bool = True,
) -> ProvisionalBandPreview | None:
    if not chapter_plans or not self.config.minimax_api_key.strip():
        return None
    self._emit_progress(
        "stage_changed",
        stage="running_provisional_preview",
        project_id=project_id,
        current_chapter=chapter_plans[0].chapter_number if chapter_plans else 0,
    )

    repo, _updater, _checker = self._make_state_helpers(session)
    preview_checker = ContinuityChecker(
        repo,
        min_chars=self.provisional_writer.min_chapter_chars,
        max_chars=self.provisional_writer.max_chapter_chars,
    )
    safe_band = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_"
        for ch in band_id
    ).strip("_") or "band"
    namespace_root = (
        f"projects/{project_id}/arcs/{arc_id}/provisional/{safe_band}"
    )
    if persist_result:
        session.query(ProvisionalChapterLedger).filter(
            ProvisionalChapterLedger.project_id == project_id,
            ProvisionalChapterLedger.arc_id == arc_id,
            ProvisionalChapterLedger.band_id == band_id,
        ).delete(synchronize_session=False)
    summaries: list[str] = []
    chapter_payloads: list[dict[str, object]] = []
    chapter_numbers: list[int] = []
    total_char_count = 0
    issue_count = 0
    failure_count = 0
    aggregate_verdict = "pass"

    for chapter_plan in chapter_plans:
        timeline_before = repo.get_current_timeline(project_id)
        current_time_label = (
            timeline_before.current_time_label
            if timeline_before is not None
            else ""
        )
        context = self.retrieval_broker.build_chapter_context(
            repo, project_id, chapter_plan
        )
        if summaries:
            previous = (
                list(context.previous_chapter_summaries)
                + summaries[-2:]
            )[-3:]
            context = context.model_copy(
                update={"previous_chapter_summaries": previous}
            )
        try:
            writer_output = self.provisional_writer.write_preview_chapter(
                context,
                trace_stage_key="provisional_preview",
                max_attempts=2,
                retry_on_timeout=True,
            )
            verdict = preview_checker.check(project_id, writer_output)
            verdict = self._normalize_provisional_verdict(writer_output, verdict)
            artifact_meta_path = ""
            draft_blob_path = ""
            if persist_result:
                artifact_paths = self.artifact_store.save_writer_output(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    writer_output=writer_output,
                    namespace_root=namespace_root,
                )
                artifact_meta_path = str(artifact_paths["meta_path"] or "")
                draft_blob_path = str(artifact_paths["writer_output"].draft_blob_path or "")
            projected_time_label = (
                writer_output.time_advance.new_time_label
                if writer_output.time_advance is not None
                else current_time_label
            )
            total_char_count += writer_output.char_count or len(writer_output.body)
            issue_count += len(verdict.issues)
            chapter_numbers.append(chapter_plan.chapter_number)
            summaries.append(
                writer_output.end_of_chapter_summary or writer_output.title
            )
            if persist_result:
                session.add(
                    ProvisionalChapterLedger(
                        id=new_id(),
                        project_id=project_id,
                        arc_id=arc_id,
                        band_id=band_id,
                        chapter_number=chapter_plan.chapter_number,
                        title=writer_output.title,
                        summary=writer_output.end_of_chapter_summary,
                        verdict=verdict.verdict,
                        char_count=writer_output.char_count,
                        artifact_meta_path=artifact_meta_path,
                        draft_blob_path=draft_blob_path,
                        current_time_label=current_time_label,
                        projected_time_label=projected_time_label,
                        state_changes_json=json.dumps(
                            [
                                change.model_dump(mode="json")
                                for change in writer_output.state_changes
                            ],
                            ensure_ascii=False,
                        ),
                        events_json=json.dumps(
                            [
                                event.model_dump(mode="json")
                                for event in writer_output.new_events
                            ],
                            ensure_ascii=False,
                        ),
                        thread_beats_json=json.dumps(
                            [
                                beat.model_dump(mode="json")
                                for beat in writer_output.thread_beats
                            ],
                            ensure_ascii=False,
                        ),
                        time_advance_json=json.dumps(
                            writer_output.time_advance.model_dump(mode="json")
                            if writer_output.time_advance is not None
                            else {},
                            ensure_ascii=False,
                        ),
                        issues_json=json.dumps(
                            [
                                issue.model_dump(mode="json")
                                for issue in verdict.issues
                            ],
                            ensure_ascii=False,
                        ),
                    )
                )
            chapter_payloads.append(
                {
                    "chapter_number": chapter_plan.chapter_number,
                    "title": writer_output.title,
                    "summary": writer_output.end_of_chapter_summary,
                    "char_count": writer_output.char_count,
                    "verdict": verdict.verdict,
                    "current_time_label": current_time_label,
                    "projected_time_label": projected_time_label,
                    "state_changes": [
                        change.model_dump(mode="json")
                        for change in writer_output.state_changes
                    ],
                    "events": [
                        event.model_dump(mode="json")
                        for event in writer_output.new_events
                    ],
                    "thread_beats": [
                        beat.model_dump(mode="json")
                        for beat in writer_output.thread_beats
                    ],
                    "time_advance": (
                        writer_output.time_advance.model_dump(mode="json")
                        if writer_output.time_advance is not None
                        else {}
                    ),
                    "artifact_meta_path": artifact_meta_path,
                    "issues": [
                        issue.model_dump(mode="json")
                        for issue in verdict.issues
                    ],
                }
            )
            if verdict.verdict == "fail":
                aggregate_verdict = "fail"
            elif verdict.verdict == "warn" and aggregate_verdict == "pass":
                aggregate_verdict = "warn"
        except Exception as exc:  # noqa: BLE001
            if self._should_degrade_provisional_preview(exc):
                fallback = self._build_provisional_fallback(
                    chapter_plan=chapter_plan,
                    current_time_label=current_time_label,
                    error_text=str(exc),
                    issue_description="当前章节预演生成失败，已降级为计划级影子草案。",
                )
                issue_count += len(fallback["issues"])
                total_char_count += int(fallback["char_count"])
                chapter_numbers.append(chapter_plan.chapter_number)
                summaries.append(str(fallback["summary"]))
                chapter_payloads.append(fallback)
                if persist_result:
                    session.add(
                        ProvisionalChapterLedger(
                            id=new_id(),
                            project_id=project_id,
                            arc_id=arc_id,
                            band_id=band_id,
                            chapter_number=chapter_plan.chapter_number,
                            title=str(fallback["title"]),
                            summary=str(fallback["summary"]),
                            verdict=str(fallback["verdict"]),
                            char_count=int(fallback["char_count"]),
                            artifact_meta_path="",
                            draft_blob_path="",
                            current_time_label=current_time_label,
                            projected_time_label=str(fallback["projected_time_label"]),
                            state_changes_json="[]",
                            events_json="[]",
                            thread_beats_json="[]",
                            time_advance_json="{}",
                            issues_json=json.dumps(fallback["issues"], ensure_ascii=False),
                            error_text=str(fallback["error"]),
                        )
                    )
                if aggregate_verdict == "pass":
                    aggregate_verdict = "warn"
                continue
            failure_count += 1
            aggregate_verdict = "fail"
            chapter_payloads.append(
                {
                    "chapter_number": chapter_plan.chapter_number,
                    "title": chapter_plan.title,
                    "summary": "",
                    "char_count": 0,
                    "verdict": "fail",
                    "error": str(exc),
                    "issues": [],
                }
            )
            if persist_result:
                session.add(
                    ProvisionalChapterLedger(
                        id=new_id(),
                        project_id=project_id,
                        arc_id=arc_id,
                        band_id=band_id,
                        chapter_number=chapter_plan.chapter_number,
                        title=chapter_plan.title,
                        summary="",
                        verdict="fail",
                        char_count=0,
                        artifact_meta_path="",
                        draft_blob_path="",
                        current_time_label=current_time_label,
                        projected_time_label=current_time_label,
                        state_changes_json="[]",
                        events_json="[]",
                        thread_beats_json="[]",
                        time_advance_json="{}",
                        issues_json="[]",
                        error_text=str(exc),
                    )
                )
            break

    artifact_path = ""
    if persist_result:
        artifact_path = self.artifact_store.save_provisional_band(
            project_id=project_id,
            arc_id=arc_id,
            band_id=band_id,
            payload={
                "project_id": project_id,
                "arc_id": arc_id,
                "band_id": band_id,
                "aggregate_verdict": aggregate_verdict,
                "preview_chapter_count": len(chapter_payloads),
                "total_char_count": total_char_count,
                "issue_count": issue_count,
                "failure_count": failure_count,
                "chapters": chapter_payloads,
            },
        )
    return ProvisionalBandPreview(
        band_id=band_id,
        artifact_path=artifact_path,
        aggregate_verdict=aggregate_verdict,
        preview_chapter_count=len(chapter_payloads),
        total_char_count=total_char_count,
        issue_count=issue_count,
        failure_count=failure_count,
        chapter_numbers=chapter_numbers,
        summary_lines=summaries,
    )

def _abort_requested(self) -> bool:
    try:
        return bool(self.should_abort and self.should_abort())
    except Exception:  # noqa: BLE001
        logger.debug("Ignoring abort predicate failure.", exc_info=True)
        return False

def _pause_requested(self) -> bool:
    try:
        return bool(self.should_pause and self.should_pause())
    except Exception:  # noqa: BLE001
        logger.debug("Ignoring pause predicate failure.", exc_info=True)
        return False

def _paused_result(
    self,
    project_id: str,
    requested_chapters: int,
    *,
    completed_chapters: list[int] | None = None,
    failed_chapters: list[int] | None = None,
    paused_chapters: list[int] | None = None,
    frozen_artifacts: list[str] | None = None,
    current_chapter: int = 0,
) -> RunResult:
    self._emit_progress(
        "stage_changed",
        stage="paused",
        project_id=project_id,
        requested_chapters=requested_chapters,
        current_chapter=current_chapter,
        completed_chapters=completed_chapters or [],
        failed_chapters=failed_chapters or [],
        paused_chapters=paused_chapters or [],
        frozen_artifacts=frozen_artifacts or [],
    )
    return RunResult(
        project_id=project_id,
        requested_chapters=requested_chapters,
        completed_chapters=list(completed_chapters or []),
        failed_chapters=list(failed_chapters or []),
        paused_chapters=list(paused_chapters or []),
        frozen_artifacts=list(frozen_artifacts or []),
        paused=True,
    )

def _cancelled_result(
    self,
    project_id: str,
    requested_chapters: int,
    *,
    completed_chapters: list[int] | None = None,
    failed_chapters: list[int] | None = None,
    paused_chapters: list[int] | None = None,
    frozen_artifacts: list[str] | None = None,
    current_chapter: int = 0,
) -> RunResult:
    self._emit_progress(
        "stage_changed",
        stage="cancelled",
        project_id=project_id,
        requested_chapters=requested_chapters,
        current_chapter=current_chapter,
        completed_chapters=completed_chapters or [],
        failed_chapters=failed_chapters or [],
        paused_chapters=paused_chapters or [],
        frozen_artifacts=frozen_artifacts or [],
    )
    return RunResult(
        project_id=project_id,
        requested_chapters=requested_chapters,
        completed_chapters=list(completed_chapters or []),
        failed_chapters=list(failed_chapters or []),
        paused_chapters=list(paused_chapters or []),
        frozen_artifacts=list(frozen_artifacts or []),
        cancelled=True,
    )

@staticmethod
def _normalize_provisional_verdict(
    writer_output: WriterOutput,
    verdict: ReviewVerdict,
) -> ReviewVerdict:
    usable_body = len((writer_output.body or "").strip()) >= 300
    filtered_issues = [
        issue for issue in verdict.issues if issue.rule_name != "char_count_low"
    ]
    if verdict.verdict != "fail":
        if len(filtered_issues) == len(verdict.issues):
            return verdict
        if not filtered_issues:
            return ReviewVerdict(verdict="pass", issues=[])
        severities = {issue.severity for issue in filtered_issues}
        next_verdict = "fail" if "error" in severities else "warn"
        return ReviewVerdict(verdict=next_verdict, issues=filtered_issues)
    if not usable_body:
        return ReviewVerdict(verdict=verdict.verdict, issues=filtered_issues)
    softened_issues = [
        issue.model_copy(update={"severity": "warning"})
        for issue in filtered_issues
    ]
    softened_issues.append(
        softened_issues[0].model_copy(
            update={
                "rule_name": "provisional_softened_fail",
                "description": "预演正文可用，已将严格失败降级为预演警告。",
                "entity_names": [],
            }
        )
        if softened_issues
        else None
    )
    softened_issues = [issue for issue in softened_issues if issue is not None]
    return ReviewVerdict(
        verdict="warn" if softened_issues else "pass",
        issues=softened_issues,
    )

@staticmethod
def _should_degrade_provisional_preview(exc: Exception) -> bool:
    text = str(exc).lower()
    if any(
        token in text
        for token in (
            "timed out",
            "timeout",
            "read operation timed out",
            "json generation failed",
            "llmjsonparseerror",
            "preview generation failed",
            "preview response body is empty",
            "connection reset",
        )
    ):
        return True
    return WritingOrchestrator._is_transient_llm_like(exc)

@staticmethod
def _build_provisional_fallback(
    *,
    chapter_plan: ChapterPlan,
    current_time_label: str,
    error_text: str,
    issue_description: str,
) -> dict[str, Any]:
    try:
        goals = json.loads(chapter_plan.goals_json or "[]") or []
    except (json.JSONDecodeError, TypeError):
        goals = []
    summary = chapter_plan.one_line.strip() or chapter_plan.title.strip() or f"第{chapter_plan.chapter_number}章"
    estimated_char_count = max(
        360,
        min(1200, 260 + len(summary) * 8 + sum(len(str(goal)) for goal in goals) * 4),
    )
    issues = [
        {
            "rule_name": "provisional_fallback",
            "severity": "warning",
            "description": issue_description,
        }
    ]
    return {
        "chapter_number": chapter_plan.chapter_number,
        "title": chapter_plan.title,
        "summary": summary,
        "char_count": estimated_char_count,
        "verdict": "warn",
        "current_time_label": current_time_label,
        "projected_time_label": current_time_label,
        "state_changes": [],
        "events": [],
        "thread_beats": [],
        "time_advance": {},
        "artifact_meta_path": "",
        "issues": issues,
        "error": error_text,
        "fallback_mode": "plan_shadow",
    }

def _load_writer_output_from_meta(self, meta_path: str) -> WriterOutput:
    payload = self.artifact_store.read_json(meta_path)
    return WriterOutput.model_validate(payload)

@staticmethod
def _load_review_verdict(review: ChapterReview) -> ReviewVerdict:
    meta = json.loads(review.review_meta_json or "{}") if getattr(review, "review_meta_json", "") else {}
    if not isinstance(meta, dict):
        meta = {}
    return ReviewVerdict.model_validate(
        {
            "verdict": review.verdict,
            "issues": json.loads(review.issues_json or "[]"),
            **meta,
        }
    )

def _seed_state(
    self,
    updater: StateUpdater,
    project_id: str,
    arc_plan: dict,
    num_chapters: int,
) -> None:
    """Seed the database with initial state from the arc plan."""
    chapters = arc_plan.get("chapters", [])
    raw_outlines = arc_plan.get("arc_outlines") or []
    normalized_outlines: list[dict[str, int | str]] = []
    cursor = 1
    for index, raw in enumerate(raw_outlines, start=1):
        if cursor > num_chapters or not isinstance(raw, dict):
            break
        raw_count = int(raw.get("chapter_count", 0) or 0)
        if raw_count <= 0:
            continue
        remaining = num_chapters - cursor + 1
        chapter_count = min(raw_count, remaining)
        if chapter_count <= 0:
            break
        chapter_start = cursor
        chapter_end = cursor + chapter_count - 1
        normalized_outlines.append(
            {
                "arc_number": index,
                "chapter_start": chapter_start,
                "chapter_end": chapter_end,
                "chapter_count": chapter_count,
                "arc_synopsis": str(raw.get("arc_synopsis", "")).strip() or str(arc_plan.get("arc_synopsis", "")).strip(),
            }
        )
        cursor = chapter_end + 1
    if not normalized_outlines:
        normalized_outlines = [
            {
                "arc_number": 1,
                "chapter_start": 1,
                "chapter_end": num_chapters,
                "chapter_count": num_chapters,
                "arc_synopsis": str(arc_plan.get("arc_synopsis", "")).strip(),
            }
        ]
    elif cursor <= num_chapters:
        normalized_outlines.append(
            {
                "arc_number": len(normalized_outlines) + 1,
                "chapter_start": cursor,
                "chapter_end": num_chapters,
                "chapter_count": num_chapters - cursor + 1,
                "arc_synopsis": f"后续弧线：第{cursor}章至第{num_chapters}章",
            }
        )

    first_arc = None
    for outline in normalized_outlines:
        chapter_start = int(outline.get("chapter_start", 1) or 1)
        chapter_end = int(outline.get("chapter_end", chapter_start) or chapter_start)
        chapter_count = max(1, int(outline.get("chapter_count", chapter_end - chapter_start + 1) or 1))
        arc = updater.create_arc_plan(
            project_id=project_id,
            arc_synopsis=str(outline.get("arc_synopsis", "") or ""),
            version=1,
            status="active" if first_arc is None else "planned",
            arc_number=int(outline.get("arc_number", 1) or 1),
            chapter_start=chapter_start,
            chapter_end=chapter_end,
            planned_target_size=chapter_count,
            planned_soft_min=max(1, int(round(chapter_count * 0.85))),
            planned_soft_max=max(chapter_count, int(round(chapter_count * 1.20))),
        )
        if first_arc is None:
            first_arc = arc
        for chapter_number in range(chapter_start, chapter_end + 1):
            ch = chapters[chapter_number - 1] if chapter_number - 1 < len(chapters) else {}
            updater.create_chapter_plan(
                project_id=project_id,
                arc_plan_id=arc.id,
                chapter_number=ch.get("chapter_number", chapter_number),
                title=ch.get("title", f"第{chapter_number}章"),
                one_line=ch.get("one_line", ""),
                goals=ch.get("goals", []),
            )

    # Entities: characters
    from forwin.characters.creation import CharacterCreationHelper
    from forwin.characters.models import CharacterCreationRequest

    character_helper = CharacterCreationHelper(updater.session)
    entity_map: dict[str, str] = {}  # name -> entity_id
    for char_data in arc_plan.get("characters", []):
        initial_state = char_data.get("initial_state", {})
        result = character_helper.create_character(
            CharacterCreationRequest(
                project_id=project_id,
                source="arc_plan_seed",
                source_ref=str(char_data.get("source_ref") or ""),
                name=char_data.get("name", "未命名"),
                description=char_data.get("description", ""),
                aliases=char_data.get("aliases", []),
                importance=char_data.get("importance", 5),
                created_at_chapter=0,
                profile={
                    "role_hint": str(char_data.get("role_hint") or ""),
                    "role_archetype": str(char_data.get("role_archetype") or char_data.get("role_hint") or ""),
                    "narrative_role": str(char_data.get("narrative_role") or ""),
                    "public_identity": str(char_data.get("public_identity") or ""),
                },
                state=initial_state if isinstance(initial_state, dict) else {},
                personality_tags=list(char_data.get("personality_tags") or []),
                audit_reason="arc plan seed character",
            )
        )
        entity_map[result.character_name] = result.legacy_entity_id or result.character_id

    # Entities: locations
    for loc_data in arc_plan.get("locations", []):
        entity = updater.create_entity(
            project_id=project_id,
            kind="location",
            name=loc_data.get("name", "未命名"),
            description=loc_data.get("description", ""),
            aliases=loc_data.get("aliases", []),
            importance=loc_data.get("importance", 5),
            chapter=0,
        )
        entity_map[entity.name] = entity.id
        initial_state = loc_data.get("initial_state", {})
        if initial_state:
            updater.create_entity_state(entity.id, 0, initial_state)

    # Entities: factions
    for fac_data in arc_plan.get("factions", []):
        entity = updater.create_entity(
            project_id=project_id,
            kind="faction",
            name=fac_data.get("name", "未命名"),
            description=fac_data.get("description", ""),
            aliases=fac_data.get("aliases", []),
            importance=fac_data.get("importance", 5),
            chapter=0,
        )
        entity_map[entity.name] = entity.id
        initial_state = fac_data.get("initial_state", {})
        if initial_state:
            updater.create_entity_state(entity.id, 0, initial_state)

    # Relations
    for rel_data in arc_plan.get("relations", []):
        source_name = rel_data.get("source_name", "")
        target_name = rel_data.get("target_name", "")
        source_id = entity_map.get(source_name)
        target_id = entity_map.get(target_name)
        if source_id and target_id:
            updater.create_relation(
                project_id=project_id,
                source_id=source_id,
                target_id=target_id,
                relation_type=rel_data.get("relation_type", "unknown"),
                description=rel_data.get("description", ""),
                chapter=0,
            )
        else:
            logger.warning(
                "Skipping relation %s -> %s: entity not found.",
                source_name,
                target_name,
            )

    # Plot threads
    for thread_data in arc_plan.get("plot_threads", []):
        updater.create_thread(
            project_id=project_id,
            name=thread_data.get("name", ""),
            description=thread_data.get("description", ""),
            priority=thread_data.get("priority", 2),
            chapter=0,
        )

    self.subworld_manager.apply_initial_arc_plan(
        session=updater.session,
        updater=updater,
        project_id=project_id,
        arc_id=first_arc.id if first_arc is not None else "",
        arc_plan=arc_plan,
        entity_map=entity_map,
    )

    # Initial timeline
    initial_time = arc_plan.get("initial_time", {})
    if initial_time:
        updater.create_time_point(
            project_id=project_id,
            label=initial_time.get("label", "故事开始"),
            ordinal=0,
            description=initial_time.get("description", ""),
        )


__all__ = ['_flush_background_llm_trace', '_compile_world_model_after_acceptance', '_run_provisional_band_preview', '_abort_requested', '_pause_requested', '_paused_result', '_cancelled_result', '_normalize_provisional_verdict', '_should_degrade_provisional_preview', '_build_provisional_fallback', '_load_writer_output_from_meta', '_load_review_verdict', '_seed_state']
