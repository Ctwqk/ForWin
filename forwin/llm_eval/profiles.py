from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from forwin.config import (
    Config,
    DEFAULT_MINIMAX_BASE_URL,
    DEFAULT_MINIMAX_MODEL,
    DEFAULT_MOONSHOT_BASE_URL,
    DEFAULT_MOONSHOT_MODEL,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
)
from forwin.runtime_settings import RuntimeSettingsStore

from .schemas import EvalProfile


DEFAULT_CODEX_CLI_BASE_URL = "codex://cli"
DEFAULT_CODEX_SPARK_MODEL = "gpt-5.3-codex-spark"


def _split_ids(value: str | list[str] | None) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _provider_from_base_url(base_url: str) -> str:
    text = str(base_url or "").lower()
    if text.startswith("codex://"):
        return "codex_cli"
    if "deepseek" in text:
        return "deepseek"
    if "moonshot" in text or "kimi" in text:
        return "moonshot"
    if "minimax" in text or "minimaxi" in text:
        return "minimax"
    return "openai_compatible"


def _profile_from_raw(raw: dict[str, Any], fallback_id: str) -> EvalProfile | None:
    profile_id = str(raw.get("id", "") or fallback_id).strip()
    base_url = str(raw.get("base_url", "")).strip()
    model = str(raw.get("model", "")).strip()
    api_key_env = str(raw.get("api_key_env", "")).strip()
    api_key = str(raw.get("api_key", "")).strip()
    if api_key_env:
        api_key = os.environ.get(api_key_env, api_key)
    if not profile_id or not base_url or not model:
        return None
    return EvalProfile(
        id=profile_id,
        name=str(raw.get("name", "") or profile_id).strip(),
        provider=str(raw.get("provider", "") or _provider_from_base_url(base_url)).strip(),
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        rate_limit_per_minute=max(1, int(raw.get("rate_limit_per_minute", 10) or 10)),
        concurrency=max(1, int(raw.get("concurrency", 1) or 1)),
        timeout_seconds=max(5.0, float(raw.get("timeout_seconds", 90.0) or 90.0)),
    )


def profile_requires_api_key(profile: EvalProfile) -> bool:
    provider = str(profile.provider or "").strip().lower()
    base_url = str(profile.base_url or "").strip().lower()
    return provider not in {"codex_cli", "codex_app", "codex_bridge"} and not base_url.startswith("codex://")


def _load_manifest_profiles(path: str) -> list[EvalProfile]:
    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("profiles", []) if isinstance(payload, dict) else []
    profiles: list[EvalProfile] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        profile = _profile_from_raw(item, f"profile-{index + 1}")
        if profile is not None:
            profiles.append(profile)
    return profiles


def _load_runtime_profiles(path: str) -> list[EvalProfile]:
    config = Config.from_env()
    if not path:
        path = config.runtime_settings_path
    store = RuntimeSettingsStore(path, env_llm_profiles=config.llm_env_profiles)
    payload = store.get()
    profiles: list[EvalProfile] = []
    for index, item in enumerate(payload.get("profiles", [])):
        if not isinstance(item, dict):
            continue
        profile = _profile_from_raw(item, f"runtime-{index + 1}")
        if profile is not None:
            profiles.append(profile)
    return profiles


def load_eval_profiles(
    *,
    manifest_path: str = "",
    runtime_settings_path: str = "",
    selected_ids: str | list[str] | None = None,
) -> list[EvalProfile]:
    selected = _split_ids(selected_ids)
    profiles = _load_manifest_profiles(manifest_path) if manifest_path else _load_runtime_profiles(runtime_settings_path)
    if selected:
        wanted = set(selected)
        profiles = [profile for profile in profiles if profile.id in wanted or profile.name in wanted]
        present = {profile.id for profile in profiles}
        for alias in selected:
            if alias in present:
                continue
            if alias == "minimax":
                profiles.append(
                    EvalProfile(
                        id="minimax",
                        name="MiniMax 默认",
                        provider="minimax",
                        base_url=os.environ.get("MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL),
                        model=os.environ.get("MINIMAX_MODEL", DEFAULT_MINIMAX_MODEL),
                        api_key=os.environ.get("MINIMAX_API_KEY", ""),
                        api_key_env="MINIMAX_API_KEY",
                    )
                )
                present.add("minimax")
            elif alias == "kimi":
                profiles.append(
                    EvalProfile(
                        id="kimi",
                        name="Kimi 默认",
                        provider="moonshot",
                        base_url=os.environ.get("KIMI_BASE_URL", os.environ.get("MOONSHOT_BASE_URL", DEFAULT_MOONSHOT_BASE_URL)),
                        model=os.environ.get("KIMI_MODEL", os.environ.get("MOONSHOT_MODEL", DEFAULT_MOONSHOT_MODEL)),
                        api_key=os.environ.get("KIMI_API_KEY", os.environ.get("MOONSHOT_API_KEY", "")),
                        api_key_env="KIMI_API_KEY",
                    )
                )
                present.add("kimi")
            elif alias == "deepseek":
                profiles.append(
                    EvalProfile(
                        id="deepseek",
                        name="DeepSeek 默认",
                        provider="deepseek",
                        base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
                        model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
                        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                        api_key_env="DEEPSEEK_API_KEY",
                    )
                )
                present.add("deepseek")
            elif alias in {"codex-spark", "spark", "codex"}:
                profiles.append(
                    EvalProfile(
                        id=alias,
                        name="GPT-5.3-Codex-Spark",
                        provider="codex_cli",
                        base_url=os.environ.get("FORWIN_CODEX_CLI_URL", DEFAULT_CODEX_CLI_BASE_URL),
                        model=os.environ.get(
                            "FORWIN_CODEX_SPARK_MODEL",
                            os.environ.get("CODEX_SPARK_MODEL", DEFAULT_CODEX_SPARK_MODEL),
                        ),
                        api_key="",
                        api_key_env="",
                        timeout_seconds=float(os.environ.get("FORWIN_CODEX_EVAL_TIMEOUT_SECONDS", "180")),
                    )
                )
                present.add(alias)
    seen: set[str] = set()
    deduped: list[EvalProfile] = []
    for profile in profiles:
        if profile.id in seen:
            continue
        seen.add(profile.id)
        deduped.append(profile)
    return deduped


def redact_profile(profile: EvalProfile) -> dict[str, Any]:
    payload = profile.model_dump(mode="json")
    if payload.get("api_key"):
        payload["api_key"] = "***"
    return payload
