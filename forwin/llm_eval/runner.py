from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from forwin.config import Config
from forwin.codex_bridge.runner import CodexExecRequest, CodexExecResult, CodexExecRunner
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.writer.llm_client import OpenAICompatibleAdapter

from .reporting import render_markdown_summary, summarize_attempts
from .schemas import EvalAttemptResult, EvalCase, EvalProfile, EvalRunConfig
from .validators import validate_output
from .variants import apply_cache_buster, variant_seed


AdapterFactory = Callable[[EvalProfile], Any]


def _json_dump_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _error_category_for_status(status_code: int) -> str:
    if status_code == 429:
        return "rate_limit"
    if status_code in {500, 502, 503, 504, 529}:
        return "provider_overload"
    if status_code in {401, 403}:
        return "auth"
    if 400 <= status_code < 500:
        return "bad_request"
    return "unknown" if status_code else "network"


def _status_from_exception(exc: BaseException) -> int:
    response = getattr(exc, "response", None)
    return int(getattr(response, "status_code", 0) or 0)


def _input_chars(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False))


def _is_codex_profile(profile: EvalProfile) -> bool:
    return str(profile.provider or "").strip().lower() in {"codex_cli", "codex_app"} or str(profile.base_url or "").startswith("codex://")


class CodexCliEvalAdapter:
    provider = "codex_cli"
    base_url = "codex://cli"

    def __init__(
        self,
        profile: EvalProfile,
        *,
        runner: CodexExecRunner | None = None,
    ) -> None:
        self.profile_id = profile.id
        self.profile_name = profile.name or profile.id
        self.model = profile.model
        self.base_url = profile.base_url or self.base_url
        self.api_key = ""
        self.timeout_seconds = max(5.0, float(profile.timeout_seconds or 180.0))
        self.runner = runner or CodexExecRunner()
        self.llm_attempt_events: list[dict[str, object]] = []

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.85,
        max_tokens: int = 16384,
        response_format: dict | None = None,
        timeout_seconds: float | None = None,
        retry_on_timeout: bool = True,  # noqa: ARG002
        task_family: str = "",
        stage_key: str = "",
        output_schema: dict | None = None,
    ) -> str:
        attempt_group_id = uuid.uuid4().hex
        started_at = time.perf_counter()
        route_schema = output_schema
        if route_schema is None and response_format and response_format.get("type") == "json_object":
            route_schema = {"type": "object"}
        schema = output_schema if self._is_codex_structured_schema(output_schema) else None
        prompt = self._prompt_from_messages(
            messages,
            task_family=task_family,
            stage_key=stage_key,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        result: CodexExecResult | None = None
        try:
            result = self.runner.run(
                CodexExecRequest(
                    prompt=prompt,
                    output_schema=schema,
                    model=self.model,
                    permission_profile="prompt_only_readonly",
                    ignore_user_config=True,
                    ephemeral=True,
                ),
                timeout_seconds=float(timeout_seconds or self.timeout_seconds),
            )
            if not result.ok:
                raise RuntimeError(result.error or f"codex exec failed with return code {result.returncode}")
            content = str(result.content or "")
            self._record_attempt(
                attempt_group_id=attempt_group_id,
                started_at=started_at,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                output_schema=route_schema,
                task_family=task_family,
                stage_key=stage_key,
                output_chars=len(content),
                result=result,
            )
            return content
        except Exception as exc:  # noqa: BLE001
            self._record_attempt(
                attempt_group_id=attempt_group_id,
                started_at=started_at,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                output_schema=route_schema,
                task_family=task_family,
                stage_key=stage_key,
                output_chars=0,
                result=result,
                error=exc,
            )
            raise

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        events = list(self.llm_attempt_events)
        self.llm_attempt_events.clear()
        return events

    def close(self) -> None:
        return None

    def _record_attempt(
        self,
        *,
        attempt_group_id: str,
        started_at: float,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        response_format: dict | None,
        output_schema: dict | None,
        task_family: str,
        stage_key: str,
        output_chars: int,
        result: CodexExecResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        status = 200 if error is None else 0
        if isinstance(error, TimeoutError) or error.__class__.__name__ == "TimeoutExpired":
            error_category = "timeout"
        elif error is None:
            error_category = ""
        else:
            error_category = "codex_cli_error"
        self.llm_attempt_events.append(
            {
                "attempt_group_id": attempt_group_id,
                "attempt_no": 1,
                "profile_id": self.profile_id,
                "profile_name": self.profile_name,
                "model": self.model,
                "base_url_host": self.base_url,
                "provider_kind": "spark",
                "backend": "codex_cli",
                "http_status": status,
                "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                "input_chars": _input_chars(messages),
                "output_chars": output_chars,
                "temperature": temperature,
                "requested_temperature": temperature,
                "max_tokens": max_tokens,
                "requested_max_tokens": max_tokens,
                "error_category": error_category,
                "error_class": error.__class__.__name__ if error is not None else "",
                "error_message": str(error) if error is not None else "",
                "retryable": False,
                "fallback_eligible": False,
                "final_failure": error is not None,
                "task_family": str(task_family or ""),
                "stage_key": str(stage_key or ""),
                "llm_task_route": OpenAICompatibleAdapter._llm_task_route(
                    task_family=task_family,
                    stage_key=stage_key,
                    response_format=response_format,
                    output_schema=output_schema,
                ),
                "codex_returncode": int(result.returncode) if result is not None else 0,
            }
        )

    @staticmethod
    def _prompt_from_messages(
        messages: list[dict],
        *,
        task_family: str,
        stage_key: str,
        temperature: float,
        max_tokens: int,
        response_format: dict | None,
    ) -> str:
        return "\n\n".join(
            [
                "# ForWin Codex Spark Eval",
                f"task_family: {task_family}",
                f"stage_key: {stage_key}",
                f"temperature: {temperature}",
                f"max_tokens: {max_tokens}",
                f"response_format: {json.dumps(response_format or {}, ensure_ascii=False)}",
                "",
                "Return only the requested final content. If JSON is requested, return a single JSON object.",
                "",
                "# Messages",
                json.dumps(messages, ensure_ascii=False, indent=2),
            ]
        )

    @staticmethod
    def _is_codex_structured_schema(schema: dict | None) -> bool:
        return isinstance(schema, dict) and isinstance(schema.get("properties"), dict)


class LLMReliabilityRunner:
    def __init__(
        self,
        config: EvalRunConfig,
        *,
        adapter_factory: AdapterFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.adapter_factory = adapter_factory or self._default_adapter_factory
        self.sleep = sleep
        self.run_dir = (
            Path(config.artifact_root)
            / "llm_eval"
            / "runs"
            / config.run_id
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.attempts_path = self.run_dir / "attempts.jsonl"
        self.full_runs_path = self.run_dir / "full_runs.jsonl"

    @staticmethod
    def _default_adapter_factory(profile: EvalProfile) -> Any:
        if _is_codex_profile(profile):
            return CodexCliEvalAdapter(profile)  # type: ignore[return-value]
        adapter = OpenAICompatibleAdapter(
            api_key=profile.api_key,
            base_url=profile.base_url,
            model=profile.model,
            timeout_seconds=profile.timeout_seconds,
            retry_attempts=1,
            fallback_profiles=[],
        )
        adapter.profile_id = profile.id
        adapter.profile_name = profile.name
        return adapter

    def run(self, *, profiles: list[EvalProfile], cases: list[EvalCase]) -> dict[str, Any]:
        attempts: list[EvalAttemptResult] = []
        for profile in profiles:
            attempts.extend(self.run_direct_cases(profile=profile, cases=cases))
            if self.config.include_mini_real_run:
                if _is_codex_profile(profile):
                    self._record_skipped_mini_real_for_profile(
                        profile,
                        reason="codex_cli_direct_eval_only",
                    )
                else:
                    self.run_mini_real_for_profile(profile)
        summary = summarize_attempts(attempts)
        summary.update(
            {
                "run_id": self.config.run_id,
                "suite": self.config.suite,
                "run_dir": str(self.run_dir),
            }
        )
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (self.run_dir / "summary.md").write_text(
            render_markdown_summary(summary),
            encoding="utf-8",
        )
        return summary

    def _record_skipped_mini_real_for_profile(self, profile: EvalProfile, *, reason: str) -> dict[str, Any]:
        payload = {
            "run_id": self.config.run_id,
            "profile_id": profile.id,
            "status": "skipped",
            "reason": reason,
        }
        _json_dump_line(self.full_runs_path, payload)
        return payload

    def run_direct_cases(
        self,
        *,
        profile: EvalProfile,
        cases: list[EvalCase],
    ) -> list[EvalAttemptResult]:
        adapter = self.adapter_factory(profile)
        results: list[EvalAttemptResult] = []
        try:
            sequence = 0
            for round_index in range(max(1, int(self.config.rounds or 1))):
                for case in cases:
                    if sequence and self.config.request_interval_seconds > 0:
                        self.sleep(self.config.request_interval_seconds)
                    seed = case.variant_seed or variant_seed(
                        self.config.run_id,
                        case.case_id,
                        profile.id,
                        round_index,
                    )
                    messages = apply_cache_buster(case.messages, run_id=self.config.run_id, variant_seed=seed)
                    started_at = time.perf_counter()
                    raw_output = ""
                    exc: BaseException | None = None
                    try:
                        raw_output = str(
                            adapter.chat(
                                messages,
                                temperature=case.temperature,
                                max_tokens=case.max_tokens,
                                response_format=case.response_format,
                                timeout_seconds=profile.timeout_seconds,
                                retry_on_timeout=True,
                                task_family=case.task_family,
                                stage_key=case.stage_key,
                                output_schema={"type": "object"} if case.response_format else None,
                            )
                        )
                    except BaseException as caught:  # noqa: BLE001
                        exc = caught
                    attempts = self._drain_attempts(adapter)
                    result = self._result_from_call(
                        profile=profile,
                        case=case,
                        messages=messages,
                        raw_output=raw_output,
                        started_at=started_at,
                        attempts=attempts,
                        exc=exc,
                    )
                    results.append(result)
                    _json_dump_line(self.attempts_path, result.model_dump(mode="json"))
                    sequence += 1
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        return results

    @staticmethod
    def _drain_attempts(adapter: Any) -> list[dict[str, Any]]:
        drain = getattr(adapter, "drain_llm_attempt_events", None)
        if not callable(drain):
            return []
        raw = drain()
        return [item for item in raw if isinstance(item, dict)]

    def _result_from_call(
        self,
        *,
        profile: EvalProfile,
        case: EvalCase,
        messages: list[dict],
        raw_output: str,
        started_at: float,
        attempts: list[dict[str, Any]],
        exc: BaseException | None,
    ) -> EvalAttemptResult:
        validation = validate_output(
            raw_output,
            expected_output_kind=case.expected_output_kind,
            schema_name=case.schema_name,
        )
        last_attempt = attempts[-1] if attempts else {}
        status = int(last_attempt.get("http_status") or _status_from_exception(exc) or 0)
        error_category = str(last_attempt.get("error_category") or "")
        if exc is not None and not error_category:
            error_category = "timeout" if isinstance(exc, httpx.TimeoutException) else _error_category_for_status(status)
        retry_count = max(0, len(attempts) - 1)
        clean_success = (
            exc is None
            and status < 400
            and validation.parse_ok
            and validation.schema_ok
            and retry_count == 0
        )
        return EvalAttemptResult(
            run_id=self.config.run_id,
            profile_id=profile.id,
            case_id=case.case_id,
            stage_key=case.stage_key,
            task_family=case.task_family,
            attempt_group_id=str(last_attempt.get("attempt_group_id") or uuid.uuid4().hex),
            http_status=status,
            error_category=error_category,
            timeout_kind=str(last_attempt.get("timeout_kind") or ""),
            duration_ms=int(last_attempt.get("duration_ms") or max(0, int((time.perf_counter() - started_at) * 1000))),
            retry_count=retry_count,
            input_chars=int(last_attempt.get("input_chars") or _input_chars(messages)),
            output_chars=int(validation.output_chars or last_attempt.get("output_chars") or 0),
            temperature=(
                float(last_attempt["temperature"])
                if "temperature" in last_attempt and last_attempt["temperature"] is not None
                else None
            ),
            requested_temperature=(
                float(last_attempt["requested_temperature"])
                if "requested_temperature" in last_attempt and last_attempt["requested_temperature"] is not None
                else None
            ),
            max_tokens=(
                int(last_attempt["max_tokens"])
                if "max_tokens" in last_attempt and last_attempt["max_tokens"] is not None
                else None
            ),
            requested_max_tokens=(
                int(last_attempt["requested_max_tokens"])
                if "requested_max_tokens" in last_attempt and last_attempt["requested_max_tokens"] is not None
                else None
            ),
            parse_ok=validation.parse_ok,
            schema_ok=validation.schema_ok,
            required_keys_missing=validation.required_keys_missing,
            output_hash=validation.normalized_output_hash,
            provider_request_id=str(last_attempt.get("provider_request_id") or ""),
            error_class=exc.__class__.__name__ if exc is not None else str(last_attempt.get("error_class") or ""),
            error_message=str(exc) if exc is not None else str(last_attempt.get("error_message") or ""),
            clean_success=clean_success,
        )

    def run_mini_real_for_profile(self, profile: EvalProfile) -> dict[str, Any]:
        if self.config.base_url:
            return self.run_remote_mini_real_for_profile(profile)
        artifact_root = self.run_dir / f"mini_real_{profile.id}_artifacts"
        started_at = time.perf_counter()
        payload: dict[str, Any] = {
            "run_id": self.config.run_id,
            "profile_id": profile.id,
            "status": "started",
            "database_url": Config.from_env().database_url,
            "artifact_root": str(artifact_root),
        }
        orchestrator = WritingOrchestrator(
            Config(
                database_url=Config.from_env().database_url,
                artifact_root=str(artifact_root),
                minimax_api_key=profile.api_key,
                minimax_base_url=profile.base_url,
                minimax_model=profile.model,
                llm_timeout_seconds=profile.timeout_seconds,
                llm_retry_attempts=1,
                llm_fallback_profiles=[],
                review_interval_chapters=0,
                min_chapter_chars=800,
                target_chapter_chars=900,
                max_chapter_chars=1200,
                writer_mode="single",
                phase4_use_llm=False,
            )
        )
        try:
            result = orchestrator.run(
                premise="主角在潮雾旧城得到一枚会记录未来声音的罗盘。",
                genre="玄幻",
                num_chapters=2,
            )
            payload.update(
                {
                    "status": "completed" if not result.failed_chapters else "partial_failed",
                    "project_id": result.project_id,
                    "completed_chapters": list(result.completed_chapters),
                    "failed_chapters": list(result.failed_chapters),
                    "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                }
            )
        except Exception as exc:  # noqa: BLE001
            payload.update(
                {
                    "status": "failed",
                    "error_class": exc.__class__.__name__,
                    "error_message": str(exc),
                    "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                }
            )
        finally:
            try:
                orchestrator.llm_client.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                orchestrator.engine.dispose()
            except Exception:  # noqa: BLE001
                pass
        _json_dump_line(self.full_runs_path, payload)
        return payload

    def run_remote_mini_real_for_profile(self, profile: EvalProfile) -> dict[str, Any]:
        base_url = self.config.base_url.rstrip("/")
        started_at = time.perf_counter()
        payload: dict[str, Any] = {
            "run_id": self.config.run_id,
            "profile_id": profile.id,
            "status": "started",
            "base_url": base_url,
            "remote": True,
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                response = client.post(
                    f"{base_url}/api/generate",
                    json={
                        "premise": "LLM eval：主角在潮雾旧城得到一枚会记录未来声音的罗盘。",
                        "genre": "玄幻",
                        "num_chapters": 2,
                        "api_key": profile.api_key,
                        "base_url": profile.base_url,
                        "model": profile.model,
                        "operation_mode": "blackbox",
                        "min_chapter_chars": 800,
                        "review_interval_chapters": 0,
                    },
                )
                response.raise_for_status()
                created = response.json()
                task_id = str(created.get("id") or created.get("task_id") or "")
                payload["task_id"] = task_id
                terminal = {"completed", "partial_failed", "failed", "needs_review", "cancelled", "paused"}
                last_task: dict[str, Any] = {}
                deadline = time.monotonic() + 900
                while task_id and time.monotonic() < deadline:
                    task_response = client.get(f"{base_url}/api/tasks/{task_id}", timeout=30.0)
                    task_response.raise_for_status()
                    last_task = task_response.json()
                    if str(last_task.get("status") or "") in terminal:
                        break
                    time.sleep(5.0)
                payload.update(
                    {
                        "status": str(last_task.get("status") or "unknown"),
                        "project_id": str(last_task.get("project_id") or ""),
                        "completed_chapters": last_task.get("completed_chapters") or [],
                        "failed_chapters": last_task.get("failed_chapters") or [],
                        "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            payload.update(
                {
                    "status": "failed",
                    "error_class": exc.__class__.__name__,
                    "error_message": str(exc),
                    "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                }
            )
        _json_dump_line(self.full_runs_path, payload)
        return payload
