from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .router import LLMCallIntent


def _token_count(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def _normalized_usage(payload: dict[str, Any]) -> dict[str, int | None] | None:
    candidates: list[dict[str, Any]] = []
    direct = payload.get("usage")
    if isinstance(direct, dict):
        candidates.append(direct)
    for event in reversed(list(payload.get("raw_events") or [])):
        if not isinstance(event, dict):
            continue
        for container in (event, event.get("payload")):
            if not isinstance(container, dict):
                continue
            for key in ("usage", "token_usage"):
                value = container.get(key)
                if isinstance(value, dict):
                    candidates.append(value)
    for usage in candidates:
        prompt_tokens = _token_count(
            usage, "prompt_tokens", "input_tokens", "input_token_count"
        )
        completion_tokens = _token_count(
            usage, "completion_tokens", "output_tokens", "output_token_count"
        )
        total_tokens = _token_count(usage, "total_tokens", "total_token_count")
        if (
            total_tokens is None
            and prompt_tokens is not None
            and completion_tokens is not None
        ):
            total_tokens = prompt_tokens + completion_tokens
        if any(
            value is not None
            for value in (prompt_tokens, completion_tokens, total_tokens)
        ):
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
    return None


def _estimate_tokens(text: str) -> int:
    value = str(text or "")
    if not value:
        return 0
    non_ascii = sum(1 for character in value if ord(character) > 127)
    ascii_chars = len(value) - non_ascii
    return max(1, int(non_ascii * 0.5 + ascii_chars * 0.25))


class CodexBridgeClient:
    def __init__(
        self,
        *,
        bridge_url: str,
        token: str = "",
        timeout_seconds: float = 900.0,
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.token = token
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.client = httpx.Client(
            timeout=httpx.Timeout(
                self.timeout_seconds, connect=min(10.0, self.timeout_seconds)
            )
        )
        self.last_call_trace: dict[str, Any] = {}

    def health(self) -> dict[str, Any]:
        response = self.client.get(f"{self.bridge_url}/health")
        response.raise_for_status()
        return response.json()

    def chat(
        self,
        messages: list[dict],
        *,
        intent: LLMCallIntent,
        temperature: float = 0.85,
        max_tokens: int = 16384,
        response_format: dict | None = None,
        timeout_seconds: float | None = None,
        model: str = "",
        **_: object,
    ) -> str:
        raw_output_schema = intent.output_schema
        json_mode = bool(
            response_format and response_format.get("type") == "json_object"
        )
        output_schema = self._structured_output_schema(raw_output_schema)
        prompt = self._prompt_from_messages(
            messages,
            task_family=intent.task_family,
            stage_key=intent.stage_key,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode or output_schema is not None,
        )
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        resolved_timeout = max(
            self.timeout_seconds,
            float(timeout_seconds or self.timeout_seconds),
        )
        request_payload = {
            "prompt": prompt,
            "output_schema": output_schema,
            "timeout_seconds": resolved_timeout,
            "permission_profile": intent.permission_profile,
            "model": str(model or "").strip(),
        }
        self.last_call_trace = {
            "model": request_payload["model"],
            "requested_model": request_payload["model"],
            "request": request_payload,
            "input_chars": len(prompt),
        }
        started_at = time.perf_counter()
        try:
            response = self.client.post(
                f"{self.bridge_url}/v1/codex/chat",
                headers=headers,
                json=request_payload,
                timeout=resolved_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_call_trace.update(
                {
                    "response": None,
                    "error": f"{exc.__class__.__name__}: {exc}",
                    "duration_ms": max(
                        0, int((time.perf_counter() - started_at) * 1000)
                    ),
                }
            )
            raise
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {"raw_response_text": str(getattr(response, "text", "") or "")}
        evidence = payload.get("detail") if isinstance(payload, dict) else None
        if not isinstance(evidence, dict):
            evidence = payload if isinstance(payload, dict) else {}
        content = str(evidence.get("content", "") or "")
        usage = _normalized_usage(evidence)
        usage_source = "codex_bridge"
        if usage is None:
            prompt_tokens = _estimate_tokens(prompt)
            completion_tokens = _estimate_tokens(content)
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            usage_source = "estimated"
        self.last_call_trace.update(
            {
                "response": payload,
                "http_status": int(getattr(response, "status_code", 0) or 0),
                "raw_response_text": str(getattr(response, "text", "") or ""),
                "raw_events": list(evidence.get("raw_events") or []),
                "returncode": int(evidence.get("returncode") or 0),
                "actual_model": str(evidence.get("actual_model") or "").strip(),
                "thread_id": str(evidence.get("thread_id") or "").strip(),
                "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                "output_chars": len(content),
                **usage,
                "usage": usage,
                "usage_source": usage_source,
            }
        )
        response.raise_for_status()
        if not evidence.get("ok", False):
            raise RuntimeError(str(evidence.get("error") or "Codex bridge call failed"))
        return content

    def submit_job(
        self,
        *,
        prompt: str,
        output_schema: dict | None = None,
        cwd: str = "",
        model: str = "",
        permission_profile: str = "prompt_only_readonly",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self.client.post(
            f"{self.bridge_url}/v1/codex/jobs",
            headers=headers,
            json={
                "prompt": prompt,
                "output_schema": output_schema,
                "timeout_seconds": timeout_seconds or self.timeout_seconds,
                "cwd": cwd,
                "model": model,
                "permission_profile": permission_profile,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok", False):
            raise RuntimeError(str(payload.get("error") or "Codex job submit failed"))
        return payload

    @staticmethod
    def _prompt_from_messages(
        messages: list[dict],
        *,
        task_family: str,
        stage_key: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        instructions = [
            "Return only the requested final content.",
            "If the user requests JSON, return a single JSON object.",
        ]
        if json_mode:
            instructions.append(
                "This invocation is in JSON mode: return only valid JSON, with no markdown or prose."
            )
        return "\n\n".join(
            [
                "# ForWin Codex Invocation",
                f"task_family: {task_family}",
                f"stage_key: {stage_key}",
                f"temperature: {temperature}",
                f"max_tokens: {max_tokens}",
                "",
                " ".join(instructions),
                "",
                "# Messages",
                json.dumps(messages, ensure_ascii=False, indent=2),
            ]
        )

    @staticmethod
    def _structured_output_schema(
        schema: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(schema, dict):
            return None
        schema_type = str(schema.get("type", "") or "").strip()
        if schema_type != "object":
            return schema
        has_shape = bool(schema.get("properties") or schema.get("required"))
        return schema if has_shape else None

    def close(self) -> None:
        self.client.close()
