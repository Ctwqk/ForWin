from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.model_adapter import ModelCapabilities

logger = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_LLM_ROUTE_POLICY_VERSION = "v3.8-stage-aware-hard-replacement"
_ATTEMPT_RECORDED_ATTR = "_forwin_llm_attempt_recorded"


class ProfileOptionsMixin:
    @classmethod
    def _effective_temperature_for_profile(
        cls,
        profile: dict[str, str],
        requested_temperature: float,
    ) -> float:
        if cls._is_kimi_k25_profile(profile):
            return 0.6
        if cls._is_minimax_profile(profile):
            return min(1.0, max(0.1, float(requested_temperature)))
        return float(requested_temperature)

    @classmethod
    def _should_send_temperature(cls, profile: dict[str, str]) -> bool:
        return not cls._is_kimi_k25_profile(profile)

    @classmethod
    def _thinking_payload_for_profile(cls, profile: dict[str, str]) -> dict[str, str] | None:
        if cls._is_kimi_k25_profile(profile):
            return {"type": "disabled"}
        return None

    @classmethod
    def _effective_messages_for_profile(
        cls,
        profile: dict[str, str],
        messages: list[dict],
    ) -> list[dict]:
        if not cls._is_minimax_profile(profile):
            return list(messages)

        system_parts: list[str] = []
        non_system_messages: list[dict] = []
        for message in messages:
            role = str(message.get("role") or "").strip()
            if role in {"system", "developer"}:
                content = message.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                if content.strip():
                    system_parts.append(content)
                continue
            non_system_messages.append(dict(message))

        if not system_parts:
            return non_system_messages

        return [
            {"role": "system", "content": "\n\n".join(system_parts)},
            *non_system_messages,
        ]

    @classmethod
    def _effective_max_tokens_for_profile(
        cls,
        profile: dict[str, str],
        requested_max_tokens: int,
    ) -> int:
        if cls._is_kimi_k25_profile(profile):
            return max(int(requested_max_tokens), 1800)
        if cls._is_minimax_profile(profile):
            return max(1, min(int(requested_max_tokens), 2048))
        return int(requested_max_tokens)

    @classmethod
    def _max_tokens_payload_key_for_profile(cls, profile: dict[str, str]) -> str:
        if cls._is_minimax_profile(profile):
            return "max_completion_tokens"
        return "max_tokens"

    @classmethod
    def _effective_response_format_for_profile(
        cls,
        profile: dict[str, str],
        response_format: dict | None,
    ) -> dict | None:
        if cls._is_minimax_profile(profile):
            return None
        return response_format

    @classmethod
    def _effective_timeout_for_profile(
        cls,
        profile: dict[str, str],
        request_timeout: httpx.Timeout,
        *,
        llm_task_route: str = "",
        explicit_timeout: bool = False,
    ) -> httpx.Timeout:
        is_kimi = cls._is_kimi_k25_profile(profile)
        is_deepseek = cls._is_deepseek_profile(profile)
        if not (is_kimi or is_deepseek):
            return request_timeout
        route = str(llm_task_route or "").strip().lower()
        if explicit_timeout:
            return request_timeout
        read_timeout = max(
            120.0,
            float(getattr(request_timeout, "read", None) or 0.0),
        )
        connect_timeout = max(
            min(10.0, read_timeout),
            float(getattr(request_timeout, "connect", None) or 0.0),
        )
        return httpx.Timeout(read_timeout, connect=connect_timeout)

    @staticmethod
    def _is_kimi_k25_profile(profile: dict[str, str]) -> bool:
        base_url = str(profile.get("base_url") or "").lower()
        model = str(profile.get("model") or "").strip().lower()
        return ("moonshot" in base_url or "kimi" in base_url) and model.startswith("kimi-k2.5")

    @staticmethod
    def _is_deepseek_profile(profile: dict[str, str]) -> bool:
        text = " ".join(
            str(profile.get(key) or "").strip().lower()
            for key in ("id", "name", "base_url", "model")
        )
        return "deepseek" in text

    @classmethod
    def _is_minimax_profile(cls, profile: dict[str, str]) -> bool:
        return cls._profile_kind(profile) == "minimax"


__all__ = [
    'ProfileOptionsMixin',
]
