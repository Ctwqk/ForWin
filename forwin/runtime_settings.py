from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL


class RuntimeSettingsStore:
    """Persist mutable runtime settings outside code and environment files."""

    def __init__(
        self,
        path: str,
        *,
        default_api_key: str = "",
        default_base_url: str = DEFAULT_MINIMAX_BASE_URL,
        default_model: str = DEFAULT_MINIMAX_MODEL,
        default_operation_mode: str = "blackbox",
        default_freeze_failed_candidates: bool = True,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._default_profile = {
            "id": "default",
            "name": "MiniMax 默认",
            "api_key": default_api_key,
            "base_url": default_base_url,
            "model": default_model,
        }
        self._defaults = {
            "profiles": [dict(self._default_profile)],
            "default_profile_id": self._default_profile["id"],
            "operation_mode": default_operation_mode,
            "freeze_failed_candidates": default_freeze_failed_candidates,
        }
        self._cache: dict[str, object] | None = None

    @staticmethod
    def _clone(payload: dict[str, object]) -> dict[str, object]:
        return json.loads(json.dumps(payload, ensure_ascii=False))

    def _normalize_profile(self, raw: object, fallback_name: str) -> dict[str, str]:
        data = raw if isinstance(raw, dict) else {}
        profile_id = str(data.get("id", "")).strip() or uuid.uuid4().hex[:12]
        name = str(data.get("name", "")).strip() or fallback_name
        return {
            "id": profile_id,
            "name": name,
            "api_key": str(data.get("api_key", "")).strip(),
            "base_url": str(data.get("base_url", "")).strip() or str(self._default_profile["base_url"]),
            "model": str(data.get("model", "")).strip() or str(self._default_profile["model"]),
        }

    def _normalize_profiles(self, raw: dict[str, object]) -> tuple[list[dict[str, str]], str]:
        items = raw.get("profiles")
        profiles: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        if isinstance(items, list):
            for index, item in enumerate(items):
                profile = self._normalize_profile(item, f"模型配置 {index + 1}")
                if profile["id"] in seen_ids:
                    continue
                seen_ids.add(profile["id"])
                profiles.append(profile)
        if not profiles:
            legacy_profile = self._normalize_profile(
                {
                    "id": "default",
                    "name": "MiniMax 默认",
                    "api_key": raw.get("api_key", self._default_profile["api_key"]),
                    "base_url": raw.get("base_url", self._default_profile["base_url"]),
                    "model": raw.get("model", self._default_profile["model"]),
                },
                "MiniMax 默认",
            )
            profiles.append(legacy_profile)
        default_profile_id = str(raw.get("default_profile_id", "")).strip()
        if not default_profile_id or default_profile_id not in {profile["id"] for profile in profiles}:
            default_profile_id = profiles[0]["id"]
        return profiles, default_profile_id

    def _with_selected_profile(self, payload: dict[str, object]) -> dict[str, object]:
        profiles = payload.get("profiles", [])
        default_profile_id = str(payload.get("default_profile_id", "")).strip()
        selected = next(
            (
                profile
                for profile in profiles
                if isinstance(profile, dict) and str(profile.get("id", "")).strip() == default_profile_id
            ),
            None,
        )
        if selected is None and profiles:
            selected = profiles[0]
            payload["default_profile_id"] = str(selected.get("id", "")).strip()
        if selected is None:
            selected = dict(self._default_profile)
            payload["profiles"] = [selected]
            payload["default_profile_id"] = selected["id"]
        payload["api_key"] = str(selected.get("api_key", "")).strip()
        payload["base_url"] = str(selected.get("base_url", "")).strip() or str(self._default_profile["base_url"])
        payload["model"] = str(selected.get("model", "")).strip() or str(self._default_profile["model"])
        return payload

    def _persist_unlocked(self, payload: dict[str, object]) -> dict[str, object]:
        payload = self._with_selected_profile(payload)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._cache = self._clone(payload)
        return self._clone(payload)

    def _load_unlocked(self) -> dict[str, object]:
        if self._cache is not None:
            return self._clone(self._cache)
        payload = self._clone(self._defaults)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
            profiles, default_profile_id = self._normalize_profiles(raw if isinstance(raw, dict) else {})
            payload["profiles"] = profiles
            payload["default_profile_id"] = default_profile_id
            payload["operation_mode"] = str(raw.get("operation_mode", payload["operation_mode"]))
            payload["freeze_failed_candidates"] = bool(
                raw.get("freeze_failed_candidates", payload["freeze_failed_candidates"])
            )
        payload = self._with_selected_profile(payload)
        self._cache = self._clone(payload)
        return self._clone(payload)

    def get(self) -> dict[str, object]:
        with self._lock:
            return self._load_unlocked()

    def save(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        profile_id: str | None = None,
        operation_mode: str | None = None,
        freeze_failed_candidates: bool | None = None,
    ) -> dict[str, object]:
        with self._lock:
            payload = self._load_unlocked()
            target_profile_id = (profile_id or "").strip() or str(payload["default_profile_id"])
            profiles = [dict(item) for item in payload.get("profiles", []) if isinstance(item, dict)]
            target = next((item for item in profiles if item["id"] == target_profile_id), None)
            if target is None:
                target = {
                    "id": target_profile_id,
                    "name": f"模型配置 {len(profiles) + 1}",
                    "api_key": "",
                    "base_url": str(self._default_profile["base_url"]),
                    "model": str(self._default_profile["model"]),
                }
                profiles.append(target)
            if api_key is not None:
                target["api_key"] = api_key.strip()
            if base_url is not None:
                target["base_url"] = base_url.strip() or str(self._default_profile["base_url"])
            if model is not None:
                target["model"] = model.strip() or str(self._default_profile["model"])
            payload["profiles"] = profiles
            payload["default_profile_id"] = target["id"]
            if operation_mode is not None:
                payload["operation_mode"] = operation_mode.strip() or self._defaults["operation_mode"]
            if freeze_failed_candidates is not None:
                payload["freeze_failed_candidates"] = bool(freeze_failed_candidates)
            return self._persist_unlocked(payload)

    def save_profile(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        model: str,
        profile_id: str | None = None,
        set_as_default: bool = False,
    ) -> dict[str, object]:
        with self._lock:
            payload = self._load_unlocked()
            profiles = [dict(item) for item in payload.get("profiles", []) if isinstance(item, dict)]
            target_profile_id = (profile_id or "").strip()
            is_new = False
            target = next((item for item in profiles if item["id"] == target_profile_id), None) if target_profile_id else None
            if target is None:
                is_new = True
                target = {
                    "id": target_profile_id or uuid.uuid4().hex[:12],
                    "name": "",
                    "api_key": "",
                    "base_url": "",
                    "model": "",
                }
                profiles.append(target)
            target["name"] = name.strip() or target["name"] or f"模型配置 {len(profiles)}"
            normalized_api_key = api_key.strip()
            if normalized_api_key or is_new:
                target["api_key"] = normalized_api_key
            target["base_url"] = base_url.strip() or str(self._default_profile["base_url"])
            target["model"] = model.strip() or str(self._default_profile["model"])
            payload["profiles"] = profiles
            if set_as_default or not str(payload.get("default_profile_id", "")).strip():
                payload["default_profile_id"] = target["id"]
            return self._persist_unlocked(payload)

    def delete_profile(self, profile_id: str) -> dict[str, object]:
        with self._lock:
            payload = self._load_unlocked()
            profiles = [dict(item) for item in payload.get("profiles", []) if isinstance(item, dict)]
            remaining = [item for item in profiles if item["id"] != profile_id]
            if len(remaining) == len(profiles):
                return self._clone(payload)
            if not remaining:
                raise ValueError("至少需要保留一条模型配置。")
            payload["profiles"] = remaining
            if str(payload.get("default_profile_id", "")).strip() == profile_id:
                payload["default_profile_id"] = remaining[0]["id"]
            return self._persist_unlocked(payload)

    def set_default_profile(self, profile_id: str) -> dict[str, object]:
        with self._lock:
            payload = self._load_unlocked()
            profiles = [dict(item) for item in payload.get("profiles", []) if isinstance(item, dict)]
            if profile_id not in {item["id"] for item in profiles}:
                raise ValueError("模型配置不存在。")
            payload["profiles"] = profiles
            payload["default_profile_id"] = profile_id
            return self._persist_unlocked(payload)
