from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from forwin.api_schema import (
    RuntimeCatalogResponse,
    runtime_catalog,
)
from forwin.models.audit import DecisionEvent
from forwin.review.decision.dashboard import build_waiting_review_breakdown


logger = logging.getLogger(__name__)


def build_handlers(
    *,
    get_config: Callable[[], Any],
    get_publisher_manager: Callable[[], Any],
    get_session: Callable[[], Any],
    render_home_page: Callable[..., str],
    render_publishers_page: Callable[..., str],
    get_memory_index: Callable[[], Any] | None = None,
) -> dict[str, Callable[..., Any]]:
    def health():
        return {
            "status": "ok",
            "embedding": _embedding_health_payload(
                config=get_config(),
                memory_index=(
                    get_memory_index() if get_memory_index is not None else None
                ),
            ),
        }

    def home_page():
        publisher_manager = get_publisher_manager()
        backend_ready = (
            publisher_manager.backend_ready_payload()
            if publisher_manager is not None
            else {"extension_api_key_configured": False}
        )
        return HTMLResponse(
            render_home_page(
                extension_api_key_configured=bool(
                    backend_ready.get("extension_api_key_configured")
                ),
                extension_install_path="browser_extension/forwin-publisher",
                rule_decision_breakdown=_load_rule_decision_breakdown(get_session),
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

    def get_runtime_catalog() -> RuntimeCatalogResponse:
        config = get_config()
        if config is None:
            raise HTTPException(503, "服务尚未初始化")
        return runtime_catalog(config)

    return {
        "health": health,
        "home_page": home_page,
        "publishers_page": publishers_page,
        "get_runtime_catalog": get_runtime_catalog,
    }


def _load_rule_decision_breakdown(
    get_session: Callable[[], Any],
) -> list[dict[str, object]]:
    session = get_session()
    try:
        rows = (
            session.execute(
                select(DecisionEvent)
                .order_by(DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
                .limit(500)
            )
            .scalars()
            .all()
        )
        return build_waiting_review_breakdown(rows)
    except Exception as exc:
        logger.warning("failed to load rule decision breakdown: %s", exc)
        return []
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()


def _embedding_health_payload(
    *, config: Any, memory_index: Any | None
) -> dict[str, object]:
    if memory_index is not None:
        status = getattr(memory_index, "embedding_status", None)
        if callable(status):
            payload = status()
            if isinstance(payload, dict):
                return {
                    "kind": str(payload.get("kind") or ""),
                    "dims": int(payload.get("dims") or 0),
                    "degraded": bool(payload.get("degraded", False)),
                    "degraded_from": str(payload.get("degraded_from") or ""),
                }
    return {
        "kind": str(getattr(config, "embedding_backend", "") or ""),
        "dims": int(getattr(config, "embedding_dims", 0) or 0),
        "degraded": False,
        "degraded_from": "",
    }
