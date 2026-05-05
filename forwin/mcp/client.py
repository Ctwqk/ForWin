from __future__ import annotations

from typing import Any

import httpx

from .models import (
    ActiveTaskCheckView,
    BlockingReasonView,
    ChapterDetailView,
    ChapterSummaryView,
    GenerationControlView,
    GenesisView,
    MutationResult,
    ProjectView,
    PromptTraceSummaryView,
    STAGE_KEY_ORDER,
    StageKey,
    StageStateView,
    TaskView,
    WorldModelConflictView,
    WorldModelExportView,
    WorldModelPageView,
    WorldModelSnapshotView,
)


class ForWinAPIClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.transport = transport

    async def health(self) -> dict[str, Any]:
        return await self._request_json("GET", "/health")

    async def project_list(self) -> list[ProjectView]:
        payload = await self._request_json("GET", "/api/projects")
        return [self._project_view(item) for item in self._ensure_list(payload)]

    async def project_get(self, project_id: str) -> ProjectView:
        payload = await self._request_json("GET", f"/api/projects/{project_id}")
        return self._project_view(payload)

    async def project_create(
        self,
        *,
        title: str,
        premise: str,
        genre: str = "玄幻",
        setting_summary: str = "",
        target_total_chapters: int = 3,
    ) -> MutationResult:
        if not title.strip():
            raise ValueError("title is required")
        if not premise.strip():
            raise ValueError("premise is required")
        if target_total_chapters < 1 or target_total_chapters > 200:
            raise ValueError("target_total_chapters must be between 1 and 200")
        payload = await self._request_json(
            "POST",
            "/api/projects",
            json={
                "title": title,
                "premise": premise,
                "genre": genre,
                "setting_summary": setting_summary,
                "target_total_chapters": target_total_chapters,
            },
        )
        project_id = str(payload.get("project_id", "")).strip()
        return MutationResult(
            ok=bool(payload.get("ok", True)),
            message=str(payload.get("message", "")),
            workspace_url=str(payload.get("workspace_url", "")),
            project=await self.project_get(project_id) if project_id else None,
            genesis=await self.genesis_get(project_id) if project_id else None,
        )

    async def genesis_get(self, project_id: str) -> GenesisView:
        payload = await self._request_json("GET", f"/api/projects/{project_id}/genesis")
        return self._genesis_view(payload)

    async def genesis_stage_generate(self, *, project_id: str, stage_key: StageKey) -> MutationResult:
        self._validate_stage_key(stage_key)
        payload = await self._request_json(
            "POST",
            f"/api/projects/{project_id}/genesis/stages/{stage_key}/generate",
            json={},
        )
        return MutationResult(
            ok=True,
            message=f"Genesis stage {stage_key} generated.",
            project=await self.project_get(project_id),
            genesis=self._genesis_view(payload),
        )

    async def genesis_stage_refine(
        self,
        *,
        project_id: str,
        stage_key: StageKey,
        instruction: str,
        target_path: str = "",
        reason: str = "",
    ) -> MutationResult:
        self._validate_stage_key(stage_key)
        if not instruction.strip():
            raise ValueError("instruction is required")
        payload = await self._request_json(
            "POST",
            f"/api/projects/{project_id}/genesis/stages/{stage_key}/refine",
            json={
                "instruction": instruction,
                "target_path": target_path,
                "reason": reason or f"mcp:{stage_key}:refine",
            },
        )
        return MutationResult(
            ok=True,
            message=f"Genesis stage {stage_key} refined.",
            project=await self.project_get(project_id),
            genesis=self._genesis_view(payload),
        )

    async def genesis_stage_lock(self, *, project_id: str, stage_key: StageKey) -> MutationResult:
        self._validate_stage_key(stage_key)
        payload = await self._request_json(
            "POST",
            f"/api/projects/{project_id}/genesis/stages/{stage_key}/lock",
        )
        return MutationResult(
            ok=True,
            message=f"Genesis stage {stage_key} locked.",
            project=await self.project_get(project_id),
            genesis=self._genesis_view(payload),
        )

    async def project_start_writing(self, *, project_id: str) -> MutationResult:
        payload = await self._request_json("POST", f"/api/projects/{project_id}/start-writing")
        task_id = str(payload.get("task_id", "")).strip()
        return MutationResult(
            ok=bool(payload.get("ok", True)),
            message=str(payload.get("message", "")),
            project=await self.project_get(project_id),
            task=await self._safe_task_get(task_id, fallback_message=str(payload.get("message", ""))),
        )

    async def project_continue_generation(
        self,
        *,
        project_id: str,
        max_chapters: int | None = None,
    ) -> MutationResult:
        if max_chapters is not None and max_chapters < 1:
            raise ValueError("max_chapters must be positive when provided")
        request_json: dict[str, Any] = {}
        if max_chapters is not None:
            request_json["max_chapters"] = int(max_chapters)
        payload = await self._request_json(
            "POST",
            f"/api/projects/{project_id}/continue-generation",
            json=request_json,
        )
        task_id = str(payload.get("task_id", "")).strip()
        return MutationResult(
            ok=True,
            message=str(payload.get("message", "")),
            project=await self.project_get(project_id),
            task=await self._safe_task_get(task_id, fallback_payload=payload),
        )

    async def task_list(self, *, limit: int = 20) -> list[TaskView]:
        normalized_limit = max(1, min(int(limit), 100))
        payload = await self._request_json("GET", "/api/tasks", params={"limit": normalized_limit})
        return [self._task_view(item) for item in self._ensure_list(payload)]

    async def task_get(self, task_id: str) -> TaskView:
        payload = await self._request_json("GET", f"/api/tasks/{task_id}")
        return self._task_view(payload)

    async def task_active_generation_check(self, *, project_id: str = "") -> ActiveTaskCheckView:
        params = {"project_id": project_id} if project_id.strip() else None
        payload = await self._request_json("GET", "/api/tasks/active-generation-check", params=params)
        return ActiveTaskCheckView.model_validate(payload)

    async def task_pause(self, *, task_id: str) -> MutationResult:
        payload = await self._request_json("POST", f"/api/tasks/{task_id}/pause")
        task = await self._safe_task_get(task_id, fallback_message=str(payload.get("message", "")))
        project = await self.project_get(task.project_id) if task is not None and task.project_id else None
        return MutationResult(
            ok=bool(payload.get("ok", True)),
            message=str(payload.get("message", "")),
            project=project,
            task=task,
        )

    async def chapter_list(self, *, project_id: str) -> list[ChapterSummaryView]:
        payload = await self._request_json("GET", f"/api/projects/{project_id}/chapters")
        return [self._chapter_summary_view(item) for item in self._ensure_list(payload)]

    async def chapter_get(self, *, project_id: str, chapter_number: int) -> ChapterDetailView:
        payload = await self._request_json("GET", f"/api/projects/{project_id}/chapters/{chapter_number}")
        return self._chapter_detail_view(payload)

    async def world_model_get(
        self,
        *,
        project_id: str,
        as_of_chapter: int | None = None,
    ) -> WorldModelSnapshotView:
        params = {"as_of_chapter": int(as_of_chapter)} if as_of_chapter is not None else None
        payload = await self._request_json(
            "GET",
            f"/api/projects/{project_id}/world-model/snapshots/latest",
            params=params,
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Expected WorldModel snapshot payload from ForWin API.")
        return WorldModelSnapshotView.model_validate(payload)

    async def world_page_get(self, *, project_id: str, page_key: str) -> WorldModelPageView:
        payload = await self._request_json("GET", f"/api/projects/{project_id}/world-model/pages/{page_key}")
        if not isinstance(payload, dict):
            raise RuntimeError("Expected WorldModel page payload from ForWin API.")
        return WorldModelPageView.model_validate(payload)

    async def world_conflict_list(self, *, project_id: str) -> list[WorldModelConflictView]:
        payload = await self._request_json("GET", f"/api/projects/{project_id}/world-model/conflicts")
        return [WorldModelConflictView.model_validate(item) for item in self._ensure_list(payload)]

    async def world_export_obsidian(
        self,
        *,
        project_id: str,
        vault_root: str = "",
    ) -> WorldModelExportView:
        payload = await self._request_json(
            "POST",
            f"/api/projects/{project_id}/world-model/export-obsidian",
            json={"vault_root": vault_root},
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Expected WorldModel export payload from ForWin API.")
        return WorldModelExportView.model_validate(payload)

    async def _safe_task_get(
        self,
        task_id: str,
        *,
        fallback_message: str = "",
        fallback_payload: dict[str, Any] | None = None,
    ) -> TaskView | None:
        if not task_id:
            return None
        try:
            return await self.task_get(task_id)
        except ValueError:
            payload = dict(fallback_payload or {})
            payload.setdefault("task_id", task_id)
            payload.setdefault("message", fallback_message)
            return self._task_view(payload)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                follow_redirects=True,
            ) as client:
                response = await client.request(method.upper(), path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"ForWin API request failed for {method.upper()} {path}: {exc}") from exc

        if response.status_code >= 500:
            raise RuntimeError(
                f"ForWin API {method.upper()} {path} failed with {response.status_code}: {self._error_message(response)}"
            )
        if response.status_code >= 400:
            raise ValueError(
                f"ForWin API {method.upper()} {path} failed with {response.status_code}: {self._error_message(response)}"
            )

        if not response.content:
            return {}
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise RuntimeError(f"ForWin API {method.upper()} {path} returned unsupported payload type.")
        return payload

    @staticmethod
    def _ensure_list(payload: dict[str, Any] | list[Any]) -> list[Any]:
        if isinstance(payload, list):
            return payload
        raise RuntimeError("Expected list payload from ForWin API.")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            text = response.text.strip()
            return text or "unknown error"
        if isinstance(payload, dict):
            for key in ("detail", "message", "error"):
                value = str(payload.get(key, "")).strip()
                if value:
                    return value
        return str(payload)

    @staticmethod
    def _validate_stage_key(stage_key: str) -> None:
        if stage_key not in STAGE_KEY_ORDER:
            raise ValueError(f"Unsupported stage_key: {stage_key}")

    def _project_view(self, raw: dict[str, Any]) -> ProjectView:
        return ProjectView(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            genre=str(raw.get("genre", "")),
            premise=str(raw.get("premise", "")),
            setting_summary=str(raw.get("setting_summary", "")),
            creation_status=str(raw.get("creation_status", "legacy")),
            active_genesis_revision_id=str(raw.get("active_genesis_revision_id", "")),
            can_start_writing=bool(raw.get("can_start_writing", False)),
            chapter_count=int(raw.get("chapter_count", 0) or 0),
            generated_chapter_count=int(raw.get("generated_chapter_count", 0) or 0),
            accepted_chapter_count=int(raw.get("accepted_chapter_count", 0) or 0),
            needs_review_chapter_count=int(raw.get("needs_review_chapter_count", 0) or 0),
            latest_stage=str(raw.get("latest_stage", "")),
            next_gate=str(raw.get("next_gate", "")),
            genesis_stage_overview=self._stage_state_list(raw.get("genesis_stage_overview") or []),
            generation_control=self._generation_control_view(raw.get("generation_control") or {}),
            blocking_reason=self._blocking_reason_view(raw.get("blocking_reason") or {}),
            chapters=[
                self._chapter_summary_view(item)
                for item in self._ensure_iterable_dicts(raw.get("chapters"))
            ],
        )

    def _genesis_view(self, raw: dict[str, Any]) -> GenesisView:
        pack = raw.get("pack") or {}
        stage_states = {}
        if isinstance(pack, dict):
            stage_states = pack.get("stage_states") or {}
        return GenesisView(
            project_id=str(raw.get("project_id", "")),
            creation_status=str(raw.get("creation_status", "creating")),
            active_genesis_revision_id=str(raw.get("active_genesis_revision_id", "")),
            revision=int(raw.get("revision", 1) or 1),
            can_start_writing=bool(raw.get("can_start_writing", False)),
            stage_states=self._stage_state_dict(stage_states),
            pack=pack if isinstance(pack, dict) else {},
            prompt_traces=[
                PromptTraceSummaryView(
                    id=str(item.get("id", "")),
                    trace_scope=str(item.get("trace_scope", "")),
                    stage_key=str(item.get("stage_key", "")),
                    template_id=str(item.get("template_id", "")),
                    created_at=str(item.get("created_at", "")),
                )
                for item in self._ensure_iterable_dicts(raw.get("prompt_traces"))
            ],
        )

    def _task_view(self, raw: dict[str, Any]) -> TaskView:
        generation_control = self._generation_control_view(raw.get("generation_control") or {})
        return TaskView(
            task_id=str(raw.get("task_id", "")),
            status=str(raw.get("status", "")),
            title=str(raw.get("title", "")),
            subtitle=str(raw.get("subtitle", "")),
            project_id=(str(raw.get("project_id", "")).strip() or None),
            message=str(raw.get("message", "")),
            error=(
                str(raw.get("error", "")).strip()
                if raw.get("error") is not None
                else None
            ) or None,
            current_stage=str(raw.get("current_stage", "")) or generation_control.current_stage,
            requested_chapters=int(raw.get("requested_chapters", 0) or 0),
            current_chapter=int(raw.get("current_chapter", 0) or 0),
            completed_chapters=self._coerce_int_list(raw.get("completed_chapters")),
            failed_chapters=self._coerce_int_list(raw.get("failed_chapters")),
            paused_chapters=self._coerce_int_list(raw.get("paused_chapters")),
            pause_requested=bool(raw.get("pause_requested", False)),
            pausable=bool(raw.get("pausable", False)),
            resumable=bool(raw.get("resumable", False)),
            terminable=bool(raw.get("terminable", False)),
            deletable=bool(raw.get("deletable", False)),
            next_gate=generation_control.next_gate,
            recovery_suggestion=str(raw.get("recovery_suggestion", "")),
            generation_control=generation_control,
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )

    @staticmethod
    def _chapter_summary_view(raw: dict[str, Any]) -> ChapterSummaryView:
        return ChapterSummaryView(
            chapter_number=int(raw.get("chapter_number", 0) or 0),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", "")),
            char_count=int(raw.get("char_count", 0) or 0),
            summary=str(raw.get("summary", "")),
            has_draft=bool(raw.get("has_draft", False)),
            has_review=bool(raw.get("has_review", False)),
            acceptance_mode=str(raw.get("acceptance_mode", "")),
            repair_attempt_count=int(raw.get("repair_attempt_count", 0) or 0),
            canon_risk_level=str(raw.get("canon_risk_level", "")),
            latest_repair_scope=str(raw.get("latest_repair_scope", "")),
        )

    def _chapter_detail_view(self, raw: dict[str, Any]) -> ChapterDetailView:
        return ChapterDetailView(
            chapter_number=int(raw.get("chapter_number", 0) or 0),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", "")),
            body=str(raw.get("body", "")),
            char_count=int(raw.get("char_count", 0) or 0),
            summary=str(raw.get("summary", "")),
            has_draft=bool(raw.get("has_draft", bool(raw.get("body", "")))),
            has_review=bool(raw.get("has_review", False)),
            version=int(raw.get("version", 1) or 1),
            acceptance_mode=str(raw.get("acceptance_mode", "")),
            repair_attempt_count=int(raw.get("repair_attempt_count", 0) or 0),
            canon_risk_level=str(raw.get("canon_risk_level", "")),
            residual_review_issues=[
                item for item in self._ensure_iterable_dicts(raw.get("residual_review_issues"))
            ],
        )

    @staticmethod
    def _blocking_reason_view(raw: dict[str, Any]) -> BlockingReasonView:
        return BlockingReasonView(
            code=str(raw.get("code", "")),
            message=str(raw.get("message", "")),
            chapter_number=int(raw.get("chapter_number", 0) or 0),
            band_id=str(raw.get("band_id", "")),
            decision_event_id=str(raw.get("decision_event_id", "")),
            detail=str(raw.get("detail", "")),
        )

    def _generation_control_view(self, raw: dict[str, Any]) -> GenerationControlView:
        return GenerationControlView(
            plan_state=str(raw.get("plan_state", "none")),
            writing_state=str(raw.get("writing_state", "not_started")),
            review_state=str(raw.get("review_state", "none")),
            current_stage=str(raw.get("current_stage", "")),
            current_chapter=int(raw.get("current_chapter", 0) or 0),
            next_chapter=int(raw.get("next_chapter", 0) or 0),
            accepted_chapters=self._coerce_int_list(raw.get("accepted_chapters")),
            planned_chapters=self._coerce_int_list(raw.get("planned_chapters")),
            failed_chapters=self._coerce_int_list(raw.get("failed_chapters")),
            pending_review_chapters=self._coerce_int_list(raw.get("pending_review_chapters")),
            can_pause=bool(raw.get("can_pause", False)),
            can_resume=bool(raw.get("can_resume", False)),
            pause_requested=bool(raw.get("pause_requested", False)),
            next_gate=str(raw.get("next_gate", "")),
            blocking_reason=self._blocking_reason_view(raw.get("blocking_reason") or {}),
        )

    def _stage_state_list(self, raw: list[Any]) -> list[StageStateView]:
        states: list[StageStateView] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            states.append(self._stage_state_view(item))
        return self._sort_stage_states(states)

    def _stage_state_dict(self, raw: dict[str, Any]) -> list[StageStateView]:
        states: list[StageStateView] = []
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item.setdefault("stage_key", key)
            states.append(self._stage_state_view(item))
        return self._sort_stage_states(states)

    @staticmethod
    def _stage_state_view(raw: dict[str, Any]) -> StageStateView:
        return StageStateView(
            stage_key=str(raw.get("stage_key", "")),
            status=str(raw.get("status", "todo")),
            locked=bool(raw.get("locked", False)),
            updated_at=str(raw.get("updated_at", "")),
            last_trace_id=str(raw.get("last_trace_id", "")),
        )

    @staticmethod
    def _sort_stage_states(states: list[StageStateView]) -> list[StageStateView]:
        order = {key: index for index, key in enumerate(STAGE_KEY_ORDER)}
        return sorted(states, key=lambda item: (order.get(item.stage_key, len(order)), item.stage_key))

    @staticmethod
    def _coerce_int_list(value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _ensure_iterable_dicts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]
