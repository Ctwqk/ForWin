from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .client import PromptJsonClient
from .schemas import COMMON_SYSTEM_PROMPT, common_output_schema
from .validation import normalize_result, validate_json_schema


class PromptJsonAnalyzer:
    name = "PromptJsonAnalyzer"
    version = "1.0"
    prompt_version = "1.0"
    user_prompt_template = "Analyze the input payload and return JSON only."
    output_schema: dict[str, Any] = common_output_schema(analyzer=name)

    def __init__(
        self,
        *,
        llm_client: object | None = None,
        min_blocking_confidence: float = 0.8,
    ) -> None:
        self.llm_client = llm_client
        self.min_blocking_confidence = float(min_blocking_confidence)

    def schema(self) -> dict[str, Any]:
        return dict(self.output_schema)

    def build_prompt(self, input_payload: dict[str, Any]) -> str:
        return (
            f"{self.user_prompt_template.strip()}\n\n"
            "Input payload JSON:\n"
            f"{json.dumps(input_payload, ensure_ascii=False, sort_keys=True, indent=2)}\n\n"
            "Return one valid JSON object matching the output schema."
        )

    def build_messages(self, input_payload: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": COMMON_SYSTEM_PROMPT},
            {"role": "user", "content": self.build_prompt(input_payload)},
        ]

    def analyze(self, input_payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(input_payload or {})
        input_hash = _input_hash(payload)
        model = str(getattr(self.llm_client, "model", "") or "")
        if self.llm_client is None:
            fallback = normalize_result(
                {
                    "analyzer": self.name,
                    "version": self.version,
                    "verdict": "uncertain",
                    "blocking": False,
                    "confidence": 0.0,
                    "summary": "Prompt JSON analyzer skipped because no llm_client was provided.",
                    "issues": [],
                    "accepted_facts": [],
                    "uncertainties": [
                        {
                            "question": "Prompt analyzer unavailable",
                            "why_uncertain": "No llm_client was configured for prompt-json analysis.",
                            "needed_context": "Run with an LLM client or deterministic mode.",
                        }
                    ],
                    "metadata": {
                        "source_mode": "prompt_json",
                        "fallback_reason": "missing_llm_client",
                    },
                },
                analyzer=self.name,
                version=self.version,
                prompt_version=self.prompt_version,
                input_hash=input_hash,
                model=model,
                min_blocking_confidence=self.min_blocking_confidence,
            )
            return self._with_schema_defaults(fallback)
        started = time.monotonic()
        try:
            parsed = PromptJsonClient(self.llm_client).complete_json(
                self.build_messages(payload),
                output_schema=self.schema(),
            )
        except Exception as exc:  # noqa: BLE001
            fallback = normalize_result(
                {
                    "analyzer": self.name,
                    "version": self.version,
                    "verdict": "uncertain",
                    "blocking": False,
                    "confidence": 0.0,
                    "summary": "Prompt JSON analyzer failed and fell back to a non-blocking uncertain result.",
                    "issues": [],
                    "accepted_facts": [],
                    "uncertainties": [
                        {
                            "question": "Prompt analyzer failed",
                            "why_uncertain": str(exc),
                            "needed_context": "Retry with a working LLM client or deterministic mode.",
                        }
                    ],
                    "metadata": {
                        "source_mode": "prompt_json",
                        "fallback_reason": "llm_call_failed",
                        "error": str(exc),
                    },
                },
                analyzer=self.name,
                version=self.version,
                prompt_version=self.prompt_version,
                input_hash=input_hash,
                model=model,
                min_blocking_confidence=self.min_blocking_confidence,
            )
            return self._with_schema_defaults(fallback)
        latency_ms = int((time.monotonic() - started) * 1000)
        normalized = normalize_result(
            parsed,
            analyzer=self.name,
            version=self.version,
            prompt_version=self.prompt_version,
            input_hash=input_hash,
            model=model,
            min_blocking_confidence=self.min_blocking_confidence,
        )
        normalized = self._with_schema_defaults(normalized)
        metadata = dict(normalized.get("metadata") or {})
        metadata.update(
            {
                "latency_ms": latency_ms,
                "json_valid": True,
                "schema_valid": True,
                "repair_attempt_count": 0,
            }
        )
        normalized["metadata"] = metadata
        validate_json_schema(normalized, self.schema())
        return normalized

    def _with_schema_defaults(self, result: dict[str, Any]) -> dict[str, Any]:
        for field in self.schema().get("required", []):
            if field in result:
                continue
            result[str(field)] = {} if str(field).endswith("_assessment") else []
        return result


def _input_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
