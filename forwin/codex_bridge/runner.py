from __future__ import annotations

import json
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CodexExecRequest:
    prompt: str
    output_schema: dict[str, Any] | None = None
    cwd: str = ""
    model: str = ""
    permission_profile: str = "prompt_only_readonly"
    ignore_user_config: bool = False
    ephemeral: bool = False


@dataclass(frozen=True)
class CodexExecResult:
    ok: bool
    content: str
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    returncode: int = 0
    error: str = ""


class CodexExecRunner:
    """Thin wrapper around the local Codex CLI.

    The bridge deliberately shells out to the user's already-authenticated
    `codex exec` instead of requiring an API key inside ForWin.
    """

    def __init__(self, *, codex_bin: str = "codex", default_cwd: str | Path = ".") -> None:
        self.codex_bin = codex_bin
        self.default_cwd = Path(default_cwd).resolve()

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
            proc = subprocess.run(
                cmd,
                input=request.prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
            events = self._parse_jsonl(proc.stdout)
            content = output_path.read_text(encoding="utf-8") if output_path.exists() else self._content_from_events(events)
            if not content:
                content = (proc.stdout or "").strip()
            return CodexExecResult(
                ok=proc.returncode == 0,
                content=content,
                raw_events=events,
                returncode=proc.returncode,
                error=(proc.stderr or "").strip(),
            )

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
            for key in ("content", "message", "text", "last_message"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            item = event.get("item")
            if isinstance(item, dict):
                value = item.get("content")
                if isinstance(value, str) and value.strip():
                    return value.strip()
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
        node_type = normalized.get("type")
        if node_type == "object":
            normalized.setdefault("additionalProperties", False)
            properties = normalized.get("properties")
            if isinstance(properties, dict):
                normalized["properties"] = {
                    key: cls._normalize_schema_node(value)
                    for key, value in properties.items()
                }
                normalized.setdefault("required", list(properties.keys()))
        elif node_type == "array" and "items" in normalized:
            normalized["items"] = cls._normalize_schema_node(normalized["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            if key in normalized:
                normalized[key] = cls._normalize_schema_node(normalized[key])
        return normalized
