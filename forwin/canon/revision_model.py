"""Credential-free frozen routes and actual transport identities for one validation."""

from __future__ import annotations

import copy
import json
from functools import wraps

from .revision_validation import revision_digest

_FIELDS = (
    "provider",
    "model",
    "profile_id",
    "stage_key",
    "task_family",
    "http_status",
    "finish_reason",
    "input_chars",
    "output_chars",
    "temperature",
    "max_tokens",
    "attempt_group_id",
    "requested_temperature",
    "requested_max_tokens",
    "timeout_seconds",
    "attempt_no",
    "duration_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "usage_source",
)


def model_identity(writer):
    client = getattr(writer, "llm_client", None)
    adapter = getattr(client, "ordinary_adapter", client)
    routes = [
        {
            "provider": str(getattr(adapter, "provider", "") or "unavailable"),
            "model": str(getattr(adapter, "model", "") or "unavailable"),
        }
    ]
    for profile in getattr(adapter, "fallback_profiles", []) or []:
        routes.append(
            {
                "provider": str(
                    profile.get("provider_kind")
                    or getattr(adapter, "provider", "")
                    or "unavailable"
                ),
                "model": str(profile.get("model") or "unavailable"),
            }
        )
    router = getattr(client, "router", None)
    if router is not None and router.codex_enabled:
        routes.append(
            {
                "provider": "codex_bridge",
                "model": str(router.codex_default_model or "unavailable"),
            }
        )
    return {
        "provider": str(getattr(client, "provider", "") or "unavailable"),
        "model": str(getattr(client, "model", "") or "unavailable"),
        "max_tokens": int(getattr(writer, "max_tokens", 0) or 0),
        "temperature": 0.2,
        "extractor_revision": "full-body-v1",
        "prompt_revision": "historical-form-v1",
        "allowed_routes": json.dumps(routes, sort_keys=True, separators=(",", ":")),
        "route_fingerprint": revision_digest(routes),
    }


class EvidenceModel:
    def __init__(self, client, frozen):
        self.client = client
        self.frozen = frozen
        self.evidence = []

    def __getattr__(self, name):
        target = getattr(self.client, name)
        if name in {"chat", "complete_json", "generate_json"}:

            @wraps(target)
            def measured(*args, **kwargs):
                return self._invoke(target, *args, **kwargs)

            return measured
        return target

    def _invoke(self, call, *args, **kwargs):
        before = len(getattr(self.client, "llm_attempt_events", []) or [])
        try:
            return call(*args, **kwargs)
        finally:
            attempts = list(getattr(self.client, "llm_attempt_events", []) or [])[
                before:
            ]
            clean = [{k: row[k] for k in _FIELDS if k in row} for row in attempts]
            if not clean or any(
                not row.get("provider") or not row.get("model") for row in clean
            ):
                raise ValueError("historical call actual model identity unavailable")
            self.evidence.extend(clean)
            allowed = {
                (row["provider"], row["model"])
                for row in json.loads(self.frozen["allowed_routes"])
            }
            if any((row["provider"], row["model"]) not in allowed for row in clean):
                raise ValueError("historical call used a model outside frozen routes")
            if any(
                row.get("finish_reason") in {"length", "max_tokens", "content_filter"}
                for row in clean
            ):
                raise ValueError("historical model output was truncated or filtered")


def isolated_writer(writer, frozen):
    result = copy.copy(writer)
    if isinstance(getattr(result, "_feedback_inputs", None), list):
        result._feedback_inputs = []
    result.llm_client = EvidenceModel(writer.llm_client, frozen)
    # Writer uses this signature to forward supported transport options.
    import inspect

    result._chat_signature = inspect.signature(result.llm_client.chat)
    return result
