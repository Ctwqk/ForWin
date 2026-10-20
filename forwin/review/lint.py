from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from forwin.protocol.context import LintSignal
from forwin.protocol.writer import WriterOutput


class LintSignalCollector:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def collect(self, writer_output: WriterOutput) -> list[LintSignal]:
        if not self.enabled:
            return []

        available_tools = [
            tool for tool in ("vale", "textlint", "languagetool") if shutil.which(tool)
        ]
        if not available_tools:
            return []

        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as handle:
            handle.write(writer_output.body or "")
            tmp_path = Path(handle.name)
        try:
            signals: list[LintSignal] = []
            for tool in available_tools:
                signals.extend(self._run_tool(tool, tmp_path))
            return signals
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _run_tool(self, tool: str, path: Path) -> list[LintSignal]:
        if tool == "vale":
            return self._run_vale(path)
        if tool == "textlint":
            return self._run_textlint(path)
        if tool == "languagetool":
            return self._run_languagetool(path)
        return []

    def _run_vale(self, path: Path) -> list[LintSignal]:
        try:
            proc = subprocess.run(
                ["vale", "--output=JSON", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return []
        if not proc.stdout.strip():
            return []
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        signals: list[LintSignal] = []
        for entries in payload.values():
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                severity = "error" if str(item.get("Severity") or "").lower() == "error" else "warning"
                signals.append(
                    LintSignal(
                        tool="vale",
                        code=str(item.get("Check") or "unknown"),
                        severity=severity,
                        message=str(item.get("Message") or "Vale finding"),
                        line=int(item.get("Line") or 0),
                        evidence_refs=[
                            "tool=vale",
                            f"line={item.get('Line', 0)}",
                            f"span={item.get('Span', [])}",
                        ],
                    )
                )
        return signals

    def _run_textlint(self, path: Path) -> list[LintSignal]:
        try:
            proc = subprocess.run(
                ["textlint", "-f", "json", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return []
        if not proc.stdout.strip():
            return []
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        signals: list[LintSignal] = []
        for file_result in payload if isinstance(payload, list) else []:
            for item in file_result.get("messages", []) if isinstance(file_result, dict) else []:
                if not isinstance(item, dict):
                    continue
                signals.append(
                    LintSignal(
                        tool="textlint",
                        code=str(item.get("ruleId") or "unknown"),
                        severity="warning",
                        message=str(item.get("message") or "textlint finding"),
                        line=int(item.get("line") or 0),
                        column=int(item.get("column") or 0),
                        evidence_refs=[
                            "tool=textlint",
                            f"line={item.get('line', 0)}",
                            f"column={item.get('column', 0)}",
                        ],
                    )
                )
        return signals

    def _run_languagetool(self, path: Path) -> list[LintSignal]:
        try:
            proc = subprocess.run(
                ["languagetool", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return []
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return [
            LintSignal(
                tool="languagetool",
                code="grammar",
                severity="warning",
                message=line,
                evidence_refs=["tool=languagetool"],
            )
            for line in lines[:20]
        ]

