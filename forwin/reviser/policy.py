from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forwin.protocol.review import normalize_repair_scope


RepairDecisionKind = Literal["pause_for_review", "repair", "final_force_accept_gate"]
REPAIR_SCOPE_SEQUENCE: tuple[str, str, str] = ("draft", "chapter_plan", "band_plan")
DEFAULT_REPAIR_MODEL_SEQUENCE: tuple[str, str, str] = (
    "deepseek-reasoner",
    "deepseek-reasoner",
    "gpt-5.3-codex-spark",
)


@dataclass(slots=True)
class RepairDecision:
    kind: RepairDecisionKind
    scope: str = ""
    attempt_no: int = 0
    max_attempts: int = 0
    reason: str = ""
    preferred_provider_kind: str = ""
    preferred_model: str = ""


class RepairPolicy:
    def __init__(
        self,
        *,
        max_attempts: int = 3,
        model_sequence: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.max_attempts = max(1, min(len(REPAIR_SCOPE_SEQUENCE), int(max_attempts or 3)))
        sequence = tuple(
            str(item or "").strip()
            for item in (model_sequence or DEFAULT_REPAIR_MODEL_SEQUENCE)
            if str(item or "").strip()
        )
        self.model_sequence = sequence or DEFAULT_REPAIR_MODEL_SEQUENCE

    def decide(
        self,
        *,
        verdict: str,
        operation_mode: str,
        attempts_completed: int,
        requested_scope: str = "",
    ) -> RepairDecision:
        if str(verdict or "") != "fail":
            return RepairDecision(kind="pause_for_review", reason="review-not-fail")
        if str(operation_mode or "") != "blackbox":
            return RepairDecision(kind="pause_for_review", reason="manual-mode")
        if attempts_completed >= self.max_attempts:
            return RepairDecision(
                kind="final_force_accept_gate",
                attempt_no=self.max_attempts,
                max_attempts=self.max_attempts,
                reason="repair-attempts-exhausted",
            )
        default_scope = REPAIR_SCOPE_SEQUENCE[min(attempts_completed, len(REPAIR_SCOPE_SEQUENCE) - 1)]
        normalized_scope = normalize_repair_scope(requested_scope, default=default_scope)
        scope = normalized_scope if normalized_scope in REPAIR_SCOPE_SEQUENCE else default_scope
        attempt_no = attempts_completed + 1
        preferred_provider_kind, preferred_model = self._model_preference_for_attempt(attempt_no)
        return RepairDecision(
            kind="repair",
            scope=scope,
            attempt_no=attempt_no,
            max_attempts=self.max_attempts,
            reason="blackbox-auto-repair",
            preferred_provider_kind=preferred_provider_kind,
            preferred_model=preferred_model,
        )

    def _model_preference_for_attempt(self, attempt_no: int) -> tuple[str, str]:
        if attempt_no <= 0:
            return "", ""
        index = min(attempt_no - 1, len(self.model_sequence) - 1)
        entry = str(self.model_sequence[index] or "").strip()
        if not entry:
            return "", ""
        provider_kind = _infer_provider_kind(entry)
        preferred_model = "" if entry.lower() in _PROVIDER_KIND_NAMES else entry
        return provider_kind, preferred_model


_PROVIDER_KIND_NAMES = {
    "spark",
    "deepseek",
    "kimi",
    "moonshot",
    "openai",
    "minimax",
    "gemini",
}


def _infer_provider_kind(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text == "moonshot":
        return "kimi"
    if text in _PROVIDER_KIND_NAMES:
        return text
    if "codex-spark" in text or text == "gpt-5.3-codex-spark":
        return "spark"
    if "deepseek" in text:
        return "deepseek"
    if "kimi" in text or "moonshot" in text:
        return "kimi"
    if "minimax" in text:
        return "minimax"
    if "gemini" in text:
        return "gemini"
    if text.startswith("gpt-") or "openai" in text:
        return "openai"
    return ""
