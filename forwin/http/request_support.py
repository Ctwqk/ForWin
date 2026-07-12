"""ForWin Web API – FastAPI interface for the novel generation system."""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.orm import Session

from forwin.api_schema import (
    BookGenesisPatchRequest,
)
from forwin.genesis import (
    BookGenesisService,
)
from forwin.config import InfrastructureConfig
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project
import forwin.models.phase  # noqa: F401
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.http.runtime import HttpRuntime


logger = logging.getLogger(__name__)


def _get_session(runtime: HttpRuntime) -> Session:
    return runtime.get_session()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _display_datetime(
    value: datetime | None,
    *,
    display_timezone: ZoneInfo | None = None,
) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    target_timezone = display_timezone or ZoneInfo("America/Los_Angeles")
    return value.astimezone(target_timezone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _json_load_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _json_load_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_dump(value: Any, fallback: Any) -> str:
    normalized = value if isinstance(value, type(fallback)) else fallback
    return json.dumps(normalized, ensure_ascii=False)


def _build_genesis_service(
    runtime: HttpRuntime,
    infrastructure: InfrastructureConfig | None = None,
    *,
    model_profile_id: str = "",
) -> BookGenesisService:
    resolved = infrastructure or runtime.config or InfrastructureConfig(minimax_api_key="")
    resolved_profile = resolved.resolve_model_profile(model_profile_id).model_dump(
        mode="python"
    )
    shared_container = (
        infrastructure is None
        and not model_profile_id
        and runtime.container is not None
    )
    policy = RuntimePolicy.for_profile(
        "standard",
        model_profile_id=str(model_profile_id or "").strip(),
    )
    container = (
        runtime.container
        if shared_container
        else RuntimeContainer.from_config(resolved, policy=policy, role="api")
    )
    service = (
        container.services().book_genesis
        if shared_container
        else container.build_book_genesis_service()
    )
    setattr(service, "_forwin_runtime_owned", True)
    setattr(
        service, "_forwin_runtime_container", None if shared_container else container
    )
    setattr(service, "_forwin_runtime_shared", bool(shared_container))
    setattr(service.llm_client, "profile_id", resolved_profile.get("id", ""))
    setattr(service.llm_client, "profile_name", resolved_profile.get("name", ""))
    return service


def _close_genesis_service(
    runtime: HttpRuntime,
    service: BookGenesisService | None,
) -> None:
    if getattr(service, "_forwin_runtime_shared", False):
        return
    client = getattr(service, "llm_client", None)
    close = getattr(client, "client", None)
    if close is not None:
        try:
            close.close()
        except Exception:  # noqa: BLE001
            logger.debug("BookGenesisService client close failed", exc_info=True)
    container = getattr(service, "_forwin_runtime_container", None)
    if container is not None and container is not runtime.container:
        try:
            container.services().engine.dispose()
        except Exception:  # noqa: BLE001
            logger.debug(
                "BookGenesisService runtime engine dispose failed", exc_info=True
            )


def _active_genesis_revision(
    session: Session, project: Project
) -> BookGenesisRevision | None:
    revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
    if not revision_id:
        return None
    return session.get(BookGenesisRevision, revision_id)


def _require_genesis_project(project: Project) -> None:
    creation_status = str(getattr(project, "creation_status", "") or "").strip()
    if creation_status and creation_status not in {
        "creating",
        "genesis_ready",
        "writing",
    }:
        raise HTTPException(400, f"项目生命周期状态无效：{creation_status}")


def _genesis_patch_payload(req: BookGenesisPatchRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "book_brief",
        "world",
        "book_arc_blueprint",
        "subworld_policy",
        "execution_bootstrap",
        "stage_states",
    ):
        value = getattr(req, key)
        if value is None:
            continue
        payload[key] = value
    return payload


def _coerce_int_list(value: Any) -> list[int]:
    numbers: list[int] = []
    for item in value if isinstance(value, list) else []:
        try:
            numbers.append(int(item))
        except (TypeError, ValueError):
            continue
    return numbers


__all__ = [name for name in globals() if not name.startswith("__")]
