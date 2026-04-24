from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .runner import CodexExecRequest, CodexExecResult, CodexExecRunner


class CodexBridgeChatRequest(BaseModel):
    prompt: str
    output_schema: dict[str, Any] | None = None
    timeout_seconds: float | None = None
    cwd: str = ""
    model: str = ""
    permission_profile: str = "prompt_only_readonly"


class CodexBridgeChatResponse(BaseModel):
    ok: bool
    content: str = ""
    backend: str = "codex_bridge"
    raw_events: list[dict[str, Any]] = Field(default_factory=list)
    returncode: int = 0
    error: str = ""


class CodexBridgeJobSubmitResponse(BaseModel):
    ok: bool = True
    job_id: str
    status: str = "queued"


class CodexBridgeJobStatusResponse(BaseModel):
    ok: bool = True
    job_id: str
    status: str
    submitted_at: str
    finished_at: str = ""
    result: dict[str, Any] | None = None
    error: str = ""


def _auth_dependency(token: str):
    def require_auth(authorization: str = Header(default="")) -> None:
        if not token:
            return
        expected = f"Bearer {token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Codex bridge token required")

    return require_auth


def _response_from_result(result: CodexExecResult) -> CodexBridgeChatResponse:
    return CodexBridgeChatResponse(
        ok=result.ok,
        content=result.content,
        raw_events=result.raw_events,
        returncode=result.returncode,
        error=result.error,
    )


def _schema_validation_error(content: str, output_schema: dict[str, Any] | None) -> str:
    if not output_schema:
        return ""
    expected_type = str(output_schema.get("type", "") or "").strip()
    if not expected_type:
        return ""
    parsed: Any = content
    if expected_type in {"object", "array"}:
        try:
            parsed = json.loads(content)
        except Exception as exc:  # noqa: BLE001
            return f"schema_parse_failed: {exc}"
    if expected_type == "object" and not isinstance(parsed, dict):
        return "schema_type_mismatch: expected object"
    if expected_type == "array" and not isinstance(parsed, list):
        return "schema_type_mismatch: expected array"
    if expected_type == "string" and not isinstance(parsed, str):
        return "schema_type_mismatch: expected string"
    if expected_type == "object":
        required = [str(item) for item in (output_schema.get("required") or []) if str(item)]
        missing = [key for key in required if key not in parsed]
        if missing:
            return f"schema_required_missing: {', '.join(missing)}"
    return ""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_app(
    *,
    token: str | None = None,
    runner: CodexExecRunner | None = None,
    max_workers: int | None = None,
) -> FastAPI:
    resolved_token = str(token if token is not None else os.environ.get("FORWIN_CODEX_BRIDGE_TOKEN", "")).strip()
    resolved_runner = runner or CodexExecRunner(default_cwd=os.environ.get("FORWIN_CODEX_DEFAULT_CWD", "."))
    executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers or os.environ.get("FORWIN_CODEX_MAX_CONCURRENT", "1"))))
    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = threading.Lock()
    app = FastAPI(title="ForWin Codex Bridge")
    require_auth = _auth_dependency(resolved_token)

    @app.get("/health")
    def health() -> dict[str, object]:
        status = resolved_runner.health()
        return {"status": "ok" if status.get("available") else "degraded", "backend": "codex_bridge", **status}

    @app.post("/v1/codex/chat", dependencies=[Depends(require_auth)])
    def chat(req: CodexBridgeChatRequest) -> CodexBridgeChatResponse:
        result = resolved_runner.run(
            CodexExecRequest(
                prompt=req.prompt,
                output_schema=req.output_schema,
                cwd=req.cwd,
                model=req.model,
                permission_profile=req.permission_profile,
            ),
            timeout_seconds=req.timeout_seconds,
        )
        validation_error = _schema_validation_error(result.content, req.output_schema) if result.ok else ""
        if validation_error:
            result = CodexExecResult(
                ok=False,
                content=result.content,
                raw_events=result.raw_events,
                returncode=result.returncode,
                error=validation_error,
            )
        if not result.ok:
            raise HTTPException(
                status_code=502,
                detail={"error": result.error or result.content, "returncode": result.returncode},
            )
        return _response_from_result(result)

    def run_job(job_id: str, req: CodexBridgeChatRequest) -> None:
        with jobs_lock:
            jobs[job_id]["status"] = "running"
        try:
            result = resolved_runner.run(
                CodexExecRequest(
                    prompt=req.prompt,
                    output_schema=req.output_schema,
                    cwd=req.cwd,
                    model=req.model,
                    permission_profile=req.permission_profile,
                ),
                timeout_seconds=req.timeout_seconds,
            )
            validation_error = _schema_validation_error(result.content, req.output_schema) if result.ok else ""
            if validation_error:
                result = CodexExecResult(
                    ok=False,
                    content=result.content,
                    raw_events=result.raw_events,
                    returncode=result.returncode,
                    error=validation_error,
                )
            with jobs_lock:
                jobs[job_id]["status"] = "succeeded" if result.ok else "failed"
                jobs[job_id]["finished_at"] = _utc_now_iso()
                jobs[job_id]["result"] = _response_from_result(result).model_dump(mode="json")
                jobs[job_id]["error"] = result.error or ("" if result.ok else result.content)
        except Exception as exc:  # noqa: BLE001
            with jobs_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["finished_at"] = _utc_now_iso()
                jobs[job_id]["error"] = f"{exc.__class__.__name__}: {exc}"

    @app.post("/v1/codex/jobs", dependencies=[Depends(require_auth)])
    def submit_job(req: CodexBridgeChatRequest) -> CodexBridgeJobSubmitResponse:
        job_id = uuid.uuid4().hex[:16]
        with jobs_lock:
            jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "submitted_at": _utc_now_iso(),
                "finished_at": "",
                "result": None,
                "error": "",
            }
        future = executor.submit(run_job, job_id, req)
        # Give very short fake/test jobs a chance to complete before immediate polling.
        future.result(timeout=0.01) if future.done() else None
        return CodexBridgeJobSubmitResponse(job_id=job_id, status=jobs[job_id]["status"])

    @app.get("/v1/codex/jobs/{job_id}", dependencies=[Depends(require_auth)])
    def get_job(job_id: str) -> CodexBridgeJobStatusResponse:
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Codex job not found")
            return CodexBridgeJobStatusResponse.model_validate(dict(job))

    return app


app = build_app()
