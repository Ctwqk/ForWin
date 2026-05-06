from __future__ import annotations

from typing import Any

from forwin.protocol.context import ChapterContextPack, LintSignal
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput


REFERENCE_MODEL_LABELS = ("MBTI", "九型", "DISC", "Socionics", "星座", "临床诊断")
UNTRIGGERED_STRESS_MARKERS = ("信息失控", "失控", "监视", "控制")


class PersonalityConsistencyReviewer:
    name = "personality"

    def review(self, context: ChapterContextPack, writer_output: WriterOutput, **_kwargs) -> ReviewVerdict:
        signals = self.collect(context, writer_output)
        issues = [
            ContinuityIssue(
                rule_name=signal.code or "personality_consistency",
                severity="error" if signal.severity == "error" else "warning",
                description=signal.message,
                reviewer=self.name,
                issue_type="personality",
                target_scope="chapter",
                evidence_refs=list(signal.evidence_refs),
                suggested_fix="保持人物行为与 active personality context 对齐。",
            )
            for signal in signals
            if signal.severity in {"error", "warning"}
        ]
        verdict = "fail" if any(issue.severity == "error" for issue in issues) else (
            "warn" if issues else "pass"
        )
        return ReviewVerdict(verdict=verdict, issues=issues)

    def collect(self, context: ChapterContextPack, writer_output: WriterOutput) -> list[LintSignal]:
        signals: list[LintSignal] = []
        signals.extend(self._integrity_signals(context))
        body = str(writer_output.body or "")
        for active_context in context.active_personality_contexts or []:
            if not isinstance(active_context, dict):
                continue
            character_id = str(active_context.get("character_id") or "").strip()
            character_name = str(active_context.get("character_name") or "").strip()
            evidence_refs = [f"personality:{character_id}"] if character_id else []
            if self._mentions_character(body, character_id, character_name) and any(label in body for label in REFERENCE_MODEL_LABELS):
                signals.append(
                    LintSignal(
                        tool="personality",
                        code="reference_model_override",
                        severity="warning",
                        message=f"{character_name or character_id} appears to be explained through a reference model label.",
                        evidence_refs=evidence_refs,
                    )
                )
            if self._mentions_character(body, character_id, character_name) and any(marker in body for marker in UNTRIGGERED_STRESS_MARKERS):
                active_skills = active_context.get("active_skills") if isinstance(active_context, dict) else {}
                stress_mode = []
                if isinstance(active_skills, dict):
                    stress_mode = [str(item) for item in active_skills.get("stress_mode") or []]
                if not stress_mode:
                    signals.append(
                        LintSignal(
                            tool="personality",
                            code="stress_mode_without_trigger",
                            severity="warning",
                            message=f"{character_name or character_id} shows stress-mode behavior without active stress evidence.",
                            evidence_refs=evidence_refs,
                        )
                    )
        return signals

    def _integrity_signals(self, context: ChapterContextPack) -> list[LintSignal]:
        signals: list[LintSignal] = []
        for issue in context.personality_integrity_issues or []:
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code") or "personality_integrity_issue")
            character_id = str(issue.get("character_id") or "").strip()
            severity = "error" if str(issue.get("severity") or "error") == "error" else "warning"
            signals.append(
                LintSignal(
                    tool="personality",
                    code=code,
                    severity=severity,
                    message=str(issue.get("message") or code),
                    evidence_refs=[f"personality:{character_id}"] if character_id else [],
                )
            )
        return signals

    def _mentions_character(self, text: str, character_id: str, character_name: str) -> bool:
        return bool(
            (character_name and character_name in text)
            or (character_id and character_id in text)
        )
