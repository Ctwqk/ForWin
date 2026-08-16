from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _run_process(
    cmd: list[str],
    *,
    input: str,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


@dataclass(frozen=True)
class CodexExecRequest:
    prompt: str
    output_schema: dict[str, Any] | None = None
    cwd: str = ""
    model: str = ""
    permission_profile: str = "prompt_only_readonly"
    ignore_user_config: bool = True
    ephemeral: bool = False


@dataclass(frozen=True)
class CodexExecResult:
    ok: bool
    content: str
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    returncode: int = 0
    error: str = ""
    actual_model: str = ""
    thread_id: str = ""


class CodexExecRunner:
    """Thin wrapper around the local Codex CLI.

    The bridge deliberately shells out to the user's already-authenticated
    `codex exec` instead of requiring an API key inside ForWin.
    """

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        default_cwd: str | Path = ".",
        codex_home: str | Path | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.default_cwd = Path(default_cwd).resolve()
        configured_home = (
            codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        )
        self.codex_home = Path(configured_home).expanduser().resolve()

    def health(self) -> dict[str, object]:
        try:
            proc = subprocess.run(
                [self.codex_bin, "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "version": "", "error": str(exc)}
        version = (proc.stdout or proc.stderr or "").strip()
        return {"available": proc.returncode == 0, "version": version, "error": proc.stderr.strip() if proc.returncode else ""}

    def run(self, request: CodexExecRequest, *, timeout_seconds: float | None = None) -> CodexExecResult:
        cwd = Path(request.cwd).resolve() if request.cwd else self.default_cwd
        with tempfile.TemporaryDirectory(prefix="forwin-codex-") as tmp:
            tmp_path = Path(tmp)
            output_path = tmp_path / "last_message.txt"
            cmd = [
                self.codex_bin,
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "-c",
                'approval_policy="never"',
                "-c",
                'model_reasoning_effort="high"',
                "--output-last-message",
                str(output_path),
                "-C",
                str(cwd),
            ]
            if request.model:
                cmd.extend(["--model", request.model])
            if request.ignore_user_config:
                cmd.append("--ignore-user-config")
            if request.ephemeral:
                cmd.append("--ephemeral")
            if request.output_schema:
                schema_path = tmp_path / "output_schema.json"
                schema_path.write_text(
                    json.dumps(self._codex_output_schema(request.output_schema), ensure_ascii=False),
                    encoding="utf-8",
                )
                cmd.extend(["--output-schema", str(schema_path)])
            cmd.append("-")
            proc = _run_process(
                cmd,
                input=request.prompt,
                timeout=timeout_seconds,
            )
            events = self._parse_jsonl(proc.stdout)
            thread_id = self._thread_id_from_events(events)
            actual_model = self._actual_model_from_session(thread_id)
            content = (
                output_path.read_text(encoding="utf-8")
                if output_path.exists()
                else self._content_from_events(events)
            ).strip()
            error = (proc.stderr or "").strip() or self._error_from_events(events)
            if not content and not error:
                error = "codex exec completed without a final message"
            return CodexExecResult(
                ok=proc.returncode == 0 and bool(content),
                content=content,
                raw_events=events,
                returncode=proc.returncode,
                error=error,
                actual_model=actual_model,
                thread_id=thread_id,
            )

    @staticmethod
    def _thread_id_from_events(events: list[dict[str, Any]]) -> str:
        for event in events:
            if str(event.get("type") or "") != "thread.started":
                continue
            thread_id = str(event.get("thread_id") or "").strip()
            if thread_id:
                return thread_id
        return ""

    def _actual_model_from_session(self, thread_id: str) -> str:
        if not thread_id:
            return ""
        sessions_root = self.codex_home / "sessions"
        if not sessions_root.exists():
            return ""
        candidates = list(sessions_root.rglob(f"*{thread_id}.jsonl"))
        if not candidates:
            return ""
        session_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        try:
            lines = session_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        actual_model = ""
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn_context":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            model = str(payload.get("model") or "").strip()
            if model:
                actual_model = model
        return actual_model

    @staticmethod
    def _parse_jsonl(raw: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    @staticmethod
    def _content_from_events(events: list[dict[str, Any]]) -> str:
        for event in reversed(events):
            for key in ("content", "text", "last_message"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            item = event.get("item")
            if isinstance(item, dict):
                for key in ("content", "text"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return ""

    @staticmethod
    def _error_from_events(events: list[dict[str, Any]]) -> str:
        for event in reversed(events):
            error = event.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            if str(event.get("type") or "") == "error":
                message = event.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
        return ""

    @classmethod
    def _codex_output_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        """Normalize a caller schema to Codex/OpenAI strict structured output rules."""
        return cls._normalize_schema_node(deepcopy(schema))

    @classmethod
    def _normalize_schema_node(cls, node: Any) -> Any:
        if isinstance(node, list):
            return [cls._normalize_schema_node(item) for item in node]
        if not isinstance(node, dict):
            return node
        normalized = dict(node)
        normalized.pop("default", None)
        for key in ("$defs", "definitions"):
            definitions = normalized.get(key)
            if isinstance(definitions, dict):
                normalized[key] = {
                    name: cls._normalize_schema_node(value)
                    for name, value in definitions.items()
                }
        node_type = normalized.get("type")
        if node_type == "object":
            normalized.setdefault("additionalProperties", False)
            properties = normalized.get("properties")
            if isinstance(properties, dict):
                normalized["properties"] = {
                    key: cls._normalize_schema_node(value)
                    for key, value in properties.items()
                }
                normalized["required"] = list(properties.keys())
        elif node_type == "array" and "items" in normalized:
            normalized["items"] = cls._normalize_schema_node(normalized["items"])
        if isinstance(normalized.get("additionalProperties"), dict):
            normalized["additionalProperties"] = cls._normalize_schema_node(
                normalized["additionalProperties"]
            )
        for key in ("anyOf", "oneOf", "allOf"):
            if key in normalized:
                normalized[key] = cls._normalize_schema_node(normalized[key])
        return normalized
