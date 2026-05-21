from __future__ import annotations

import json
from typing import Any

import httpx

from .router import LLMCallIntent


class CodexBridgeClient:
    def __init__(
        self,
        *,
        bridge_url: str,
        token: str = "",
        timeout_seconds: float = 90.0,
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.token = token
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.client = httpx.Client(timeout=httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds)))

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
        **_: object,
    ) -> str:
        raw_output_schema = intent.output_schema
        json_mode = bool(response_format and response_format.get("type") == "json_object")
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
        response = self.client.post(
            f"{self.bridge_url}/v1/codex/chat",
            headers=headers,
            json={
                "prompt": prompt,
                "output_schema": output_schema,
                "timeout_seconds": timeout_seconds or self.timeout_seconds,
                "permission_profile": intent.permission_profile,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok", False):
            raise RuntimeError(str(payload.get("error") or "Codex bridge call failed"))
        return str(payload.get("content", "") or "")

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
            instructions.append("This invocation is in JSON mode: return only valid JSON, with no markdown or prose.")
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
    def _structured_output_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(schema, dict):
            return None
        schema_type = str(schema.get("type", "") or "").strip()
        if schema_type != "object":
            return schema
        has_shape = bool(schema.get("properties") or schema.get("required"))
        return schema if has_shape else None

    def close(self) -> None:
        self.client.close()
