from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from forwin.api_schemas import (
    CodexBridgeStatusResponse,
    GenerateRequest,
    LLMDefaultProfileRequest,
    LLMPreferencesRequest,
    LLMProfileUpsertRequest,
    LLMSettingsRequest,
)
from forwin.llm.codex_client import CodexBridgeClient
from forwin.models.governance import DecisionEvent
from forwin.models.project import Project
from forwin.review_engine.dashboard import build_waiting_review_breakdown


logger = logging.getLogger(__name__)


def build_handlers(
    *,
    get_config: Callable[[], Any],
    get_runtime_settings: Callable[[], Any],
    get_publisher_manager: Callable[[], Any],
    get_session: Callable[[], Any],
    render_home_page: Callable[..., str],
    render_publishers_page: Callable[..., str],
    build_home_page_settings: Callable[..., dict[str, object]],
    build_runtime_config: Callable[..., Any],
    copy_config: Callable[..., Any],
    create_generation_task: Callable[..., str],
    serialize_task: Callable[..., Any],
    get_generation_task_or_404: Callable[[str], dict[str, Any]],
    project_has_active_generation_task: Callable[..., bool],
    generation_task_conflict_message: Callable[[str], str],
    resolve_project_governance: Callable[..., Any],
    governance_request_payload: Callable[[object], dict[str, object]],
    serialize_llm_settings: Callable[..., Any],
    active_generation_task_error_cls: type[Exception],
) -> dict[str, Callable[..., Any]]:
    def health():
        return {"status": "ok"}

    def home_page():
        settings = build_home_page_settings(
            base_config=get_config(),
            runtime_settings=get_runtime_settings(),
        )
        publisher_manager = get_publisher_manager()
        backend_ready = (
            publisher_manager.backend_ready_payload()
            if publisher_manager is not None
            else {"extension_api_key_configured": False}
        )
        return HTMLResponse(
            render_home_page(
                has_api_key=bool(settings["api_key"]),
                base_url=str(settings["base_url"]),
                model=str(settings["model"]),
                operation_mode=str(settings["operation_mode"]),
                freeze_failed_candidates=bool(settings["freeze_failed_candidates"]),
                min_chapter_chars=max(500, int(settings.get("min_chapter_chars", 2500))),
                review_interval_chapters=max(0, int(settings.get("review_interval_chapters", 0))),
                extension_api_key_configured=bool(backend_ready.get("extension_api_key_configured")),
                extension_install_path="browser_extension/forwin-publisher",
                review_engine_breakdown=_load_review_engine_breakdown(get_session),
            )
        )

    def publishers_page():
        publisher_manager = get_publisher_manager()
        backend_ready = (
            publisher_manager.backend_ready_payload()
            if publisher_manager is not None
            else {"extension_api_key_configured": False}
        )
        return HTMLResponse(
            render_publishers_page(
                backend_ready=backend_ready,
                extension_install_path="browser_extension/forwin-publisher",
            )
        )

    def generate(req: GenerateRequest):
        config = get_config()
        if not config:
            raise HTTPException(503, "服务尚未初始化")

        runtime_config = build_runtime_config(
            req,
            base_config=config,
            runtime_settings=get_runtime_settings(),
        )
        if not runtime_config.minimax_api_key:
            raise HTTPException(400, "MINIMAX_API_KEY 未设置。请在页面填写 API Key，或通过环境变量配置。")

        normalized_project_id = str(req.project_id or "").strip()
        task_title = (req.premise or "").strip()[:36] or "未命名生成任务"
        task_subtitle = f"{req.genre} · {req.num_chapters} 章"
        if normalized_project_id:
            session = get_session()
            try:
                project = session.get(Project, normalized_project_id)
                if project is None:
                    raise HTTPException(404, "项目不存在")
                if str(project.creation_status or "") in {"creating", "genesis_ready"}:
                    raise HTTPException(409, "该项目仍在 Genesis 阶段，请先完成创世并点击“启动写作”。")
                if project_has_active_generation_task(normalized_project_id, session=session):
                    raise HTTPException(409, generation_task_conflict_message(normalized_project_id))
                governance = resolve_project_governance(
                    project,
                    overrides=governance_request_payload(req),
                    base_config=config,
                )
                runtime_config = copy_config(
                    runtime_config,
                    operation_mode=governance.default_operation_mode,
                    review_interval_chapters=governance.review_interval_chapters,
                    progression_mode=governance.progression_mode,
                    auto_band_checkpoint=governance.auto_band_checkpoint,
                    band_warn_action=governance.band_warn_action,
                    manual_checkpoints_enabled=governance.manual_checkpoints_enabled,
                    future_constraints_enabled=governance.future_constraints_enabled,
                )
                task_title = project.title or task_title
                task_subtitle = f"书本生成 · {project.genre} · {req.num_chapters} 章"
            finally:
                session.close()

        try:
            task_id = create_generation_task(
                premise=req.premise,
                genre=req.genre,
                num_chapters=req.num_chapters,
                runtime_config=runtime_config,
                project_id=normalized_project_id,
                title=task_title,
                subtitle=task_subtitle,
                model_profile_id=str(req.model_profile_id or "").strip(),
            )
        except active_generation_task_error_cls as exc:
            raise HTTPException(409, str(exc)) from exc
        return serialize_task(task_id, get_generation_task_or_404(task_id))

    def get_llm_settings():
        runtime_settings = get_runtime_settings()
        if not runtime_settings:
            raise HTTPException(503, "服务尚未初始化")
        payload = runtime_settings.get()
        return serialize_llm_settings(payload, message="已读取当前默认模型配置")

    def save_llm_settings(req: LLMSettingsRequest):
        runtime_settings = get_runtime_settings()
        if not runtime_settings:
            raise HTTPException(503, "服务尚未初始化")
        payload = runtime_settings.save(
            api_key=req.api_key,
            base_url=req.base_url,
            model=req.model,
            operation_mode=req.operation_mode,
            freeze_failed_candidates=req.freeze_failed_candidates,
            min_chapter_chars=req.min_chapter_chars,
            review_interval_chapters=req.review_interval_chapters,
            progression_mode=req.progression_mode,
            auto_band_checkpoint=req.auto_band_checkpoint,
            band_warn_action=req.band_warn_action,
            manual_checkpoints_enabled=req.manual_checkpoints_enabled,
            future_constraints_enabled=req.future_constraints_enabled,
        )
        return serialize_llm_settings(payload, message="默认模型配置已保存")

    def save_llm_preferences(req: LLMPreferencesRequest):
        runtime_settings = get_runtime_settings()
        if not runtime_settings:
            raise HTTPException(503, "服务尚未初始化")
        payload = runtime_settings.save(
            operation_mode=req.operation_mode,
            freeze_failed_candidates=req.freeze_failed_candidates,
            min_chapter_chars=req.min_chapter_chars,
            review_interval_chapters=req.review_interval_chapters,
            progression_mode=req.progression_mode,
            auto_band_checkpoint=req.auto_band_checkpoint,
            band_warn_action=req.band_warn_action,
            manual_checkpoints_enabled=req.manual_checkpoints_enabled,
            future_constraints_enabled=req.future_constraints_enabled,
        )
        return serialize_llm_settings(payload, message="运行偏好已保存")

    def save_llm_profile(req: LLMProfileUpsertRequest):
        runtime_settings = get_runtime_settings()
        if not runtime_settings:
            raise HTTPException(503, "服务尚未初始化")
        payload = runtime_settings.save_profile(
            profile_id=req.profile_id,
            name=req.name,
            api_key=req.api_key,
            base_url=req.base_url,
            model=req.model,
            set_as_default=req.set_as_default,
        )
        return serialize_llm_settings(payload, message="模型配置已保存")

    def set_default_llm_profile(req: LLMDefaultProfileRequest):
        runtime_settings = get_runtime_settings()
        if not runtime_settings:
            raise HTTPException(503, "服务尚未初始化")
        try:
            payload = runtime_settings.set_default_profile(req.profile_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return serialize_llm_settings(payload, message="默认模型已切换")

    def delete_llm_profile(profile_id: str):
        runtime_settings = get_runtime_settings()
        if not runtime_settings:
            raise HTTPException(503, "服务尚未初始化")
        try:
            payload = runtime_settings.delete_profile(profile_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return serialize_llm_settings(payload, message="模型配置已删除")

    def get_codex_bridge_status() -> CodexBridgeStatusResponse:
        config = get_config()
        enabled = bool(getattr(config, "codex_enabled", False)) if config is not None else False
        bridge_url = str(getattr(config, "codex_bridge_url", "") or "").strip() if config is not None else ""
        if not enabled:
            return CodexBridgeStatusResponse(
                enabled=False,
                bridge_url=bridge_url,
                healthy=False,
                status="disabled",
                message="Codex Bridge 未启用。",
            )
        if not bridge_url:
            return CodexBridgeStatusResponse(
                enabled=True,
                bridge_url="",
                healthy=False,
                status="misconfigured",
                message="FORWIN_CODEX_BRIDGE_URL 未配置。",
            )
        client = CodexBridgeClient(
            bridge_url=bridge_url,
            token=str(getattr(config, "codex_bridge_token", "") or ""),
            timeout_seconds=min(15.0, float(getattr(config, "codex_sync_timeout_seconds", 90) or 90)),
        )
        try:
            health = client.health()
            backend = str(health.get("backend", "") or "").strip()
            if backend != "codex_bridge":
                return CodexBridgeStatusResponse(
                    enabled=True,
                    bridge_url=bridge_url,
                    healthy=False,
                    status="wrong_backend",
                    message="FORWIN_CODEX_BRIDGE_URL 未返回 Codex Bridge health payload。",
                    health=health,
                )
            healthy = bool(health.get("available", False) or health.get("status") == "ok")
            return CodexBridgeStatusResponse(
                enabled=True,
                bridge_url=bridge_url,
                healthy=healthy,
                status=str(health.get("status", "ok" if healthy else "degraded") or ""),
                message="Codex Bridge 可用。" if healthy else "Codex Bridge 返回 degraded。",
                health=health,
            )
        except Exception as exc:  # noqa: BLE001
            return CodexBridgeStatusResponse(
                enabled=True,
                bridge_url=bridge_url,
                healthy=False,
                status="unreachable",
                message=f"{exc.__class__.__name__}: {exc}",
            )
        finally:
            client.close()

    return {
        "health": health,
        "home_page": home_page,
        "publishers_page": publishers_page,
        "generate": generate,
        "get_llm_settings": get_llm_settings,
        "save_llm_settings": save_llm_settings,
        "save_llm_preferences": save_llm_preferences,
        "save_llm_profile": save_llm_profile,
        "set_default_llm_profile": set_default_llm_profile,
        "delete_llm_profile": delete_llm_profile,
        "get_codex_bridge_status": get_codex_bridge_status,
    }


def _load_review_engine_breakdown(get_session: Callable[[], Any]) -> list[dict[str, object]]:
    session = get_session()
    try:
        rows = session.execute(
            select(DecisionEvent)
            .order_by(DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
            .limit(500)
        ).scalars().all()
        return build_waiting_review_breakdown(rows)
    except Exception as exc:
        logger.warning("failed to load review engine decision breakdown: %s", exc)
        return []
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()
