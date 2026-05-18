from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.skills.policy import normalize_skill_strictness
from forwin.writer.profile import WriterProfile


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
        default_min_chapter_chars: int = 2500,
        default_review_interval_chapters: int = 0,
        default_progression_mode: str = "serial_canon_band_guard",
        default_auto_band_checkpoint: bool = True,
        default_band_warn_action: str = "pause",
        default_manual_checkpoints_enabled: bool = True,
        default_future_constraints_enabled: bool = True,
        default_skill_runtime_enabled: bool = True,
        default_skill_registry_path: str = "forwin_skills",
        default_skill_strictness: str = "normal",
        default_enabled_skill_groups: list[str] | None = None,
        default_disabled_skill_ids: list[str] | None = None,
        default_writer_profile: dict[str, object] | None = None,
        env_llm_profiles: list[dict[str, str]] | None = None,
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
            "min_chapter_chars": self._normalize_min_chapter_chars(default_min_chapter_chars, fallback=2500),
            "review_interval_chapters": self._normalize_review_interval(
                default_review_interval_chapters,
                fallback=0,
            ),
            "progression_mode": self._normalize_progression_mode(default_progression_mode),
            "auto_band_checkpoint": bool(default_auto_band_checkpoint),
            "band_warn_action": self._normalize_band_warn_action(default_band_warn_action),
            "manual_checkpoints_enabled": bool(default_manual_checkpoints_enabled),
            "future_constraints_enabled": bool(default_future_constraints_enabled),
            "skill_runtime_enabled": bool(default_skill_runtime_enabled),
            "skill_registry_path": str(default_skill_registry_path or "forwin_skills").strip() or "forwin_skills",
            "skill_strictness": normalize_skill_strictness(default_skill_strictness),
            "enabled_skill_groups": self._normalize_string_list(default_enabled_skill_groups),
            "disabled_skill_ids": self._normalize_string_list(default_disabled_skill_ids),
            "writer_profile": self._normalize_writer_profile(default_writer_profile),
        }
        self._env_profiles = self._normalize_env_profiles(env_llm_profiles)
        self._cache: dict[str, object] | None = None

    @staticmethod
    def _clone(payload: dict[str, object]) -> dict[str, object]:
        return json.loads(json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _normalize_min_chapter_chars(value: object, *, fallback: int) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = int(fallback)
        return max(500, min(normalized, 50000))

    @staticmethod
    def _normalize_review_interval(value: object, *, fallback: int) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = int(fallback)
        return max(0, min(normalized, 200))

    @staticmethod
    def _normalize_progression_mode(value: object) -> str:
        normalized = str(value or "").strip()
        if normalized in {"legacy_relaxed", "serial_canon", "serial_canon_band_guard"}:
            return normalized
        return "serial_canon_band_guard"

    @staticmethod
    def _normalize_band_warn_action(value: object) -> str:
        normalized = str(value or "").strip()
        return normalized if normalized in {"pause", "continue"} else "pause"

    @staticmethod
    def _normalize_string_list(value: object) -> list[str]:
        if isinstance(value, list):
            return [
                str(item).strip()
                for item in value
                if str(item).strip()
            ]
        text = str(value or "").strip()
        if not text:
            return []
        return [
            item.strip()
            for item in text.split(",")
            if item.strip()
        ]

    @staticmethod
    def _normalize_writer_profile(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return WriterProfile.from_values(**value).model_dump(mode="json")
        return WriterProfile().model_dump(mode="json")

    def _normalize_profile(self, raw: object, fallback_name: str) -> dict[str, str]:
        data = raw if isinstance(raw, dict) else {}
        profile_id = str(data.get("id", "")).strip() or uuid.uuid4().hex[:12]
        name = str(data.get("name", "")).strip() or fallback_name
        profile = {
            "id": profile_id,
            "name": name,
            "api_key": str(data.get("api_key", "")).strip(),
            "base_url": str(data.get("base_url", "")).strip() or str(self._default_profile["base_url"]),
            "model": str(data.get("model", "")).strip() or str(self._default_profile["model"]),
        }
        source = str(data.get("source", "")).strip()
        if source:
            profile["source"] = source
        return profile

    def _normalize_env_profiles(self, raw_profiles: object) -> list[dict[str, str]]:
        if not isinstance(raw_profiles, list):
            return []
        profiles: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_profiles):
            profile = self._normalize_profile(item, f".env 模型配置 {index + 1}")
            if not profile["api_key"] or not profile["base_url"] or not profile["model"]:
                continue
            if profile["id"] in seen:
                continue
            seen.add(profile["id"])
            profile["source"] = "env"
            profiles.append(profile)
        return profiles

    @staticmethod
    def _profile_kind(profile: dict[str, str]) -> str:
        text = " ".join(
            str(profile.get(key) or "").strip().lower()
            for key in ("id", "name", "base_url", "model")
        )
        if "deepseek" in text:
            return "deepseek"
        if "kimi" in text or "moonshot" in text:
            return "kimi"
        if "minimax" in text or "minimaxi" in text:
            return "minimax"
        return ""

    def _merge_env_profiles(
        self,
        profiles: list[dict[str, str]],
        default_profile_id: str,
    ) -> tuple[list[dict[str, str]], str]:
        if not self._env_profiles:
            return profiles, default_profile_id

        env_by_kind = {
            self._profile_kind(profile): profile
            for profile in self._env_profiles
            if self._profile_kind(profile)
        }
        env_by_id = {profile["id"]: profile for profile in self._env_profiles}
        merged: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for profile in profiles:
            profile_id = str(profile.get("id", "")).strip()
            kind = self._profile_kind(profile)
            replacement = env_by_kind.get(kind) if kind else env_by_id.get(profile_id)
            if replacement is not None:
                if profile_id == default_profile_id:
                    default_profile_id = replacement["id"]
                continue
            if profile_id in seen_ids:
                continue
            seen_ids.add(profile_id)
            merged.append(profile)

        for profile in self._env_profiles:
            profile_id = str(profile.get("id", "")).strip()
            if not profile_id or profile_id in seen_ids:
                continue
            seen_ids.add(profile_id)
            merged.append(dict(profile))

        if default_profile_id not in seen_ids and merged:
            default_profile_id = merged[0]["id"]
        return merged, default_profile_id

    @staticmethod
    def _strip_env_profiles_for_disk(payload: dict[str, object]) -> dict[str, object]:
        disk_payload = json.loads(json.dumps(payload, ensure_ascii=False))
        profiles = [
            {
                key: value
                for key, value in dict(item).items()
                if key != "source"
            }
            for item in disk_payload.get("profiles", [])
            if isinstance(item, dict) and str(item.get("source", "")).strip() != "env"
        ]
        disk_payload["profiles"] = profiles
        env_selected = str(disk_payload.get("default_profile_id", "")).strip() not in {
            str(item.get("id", "")).strip()
            for item in profiles
            if isinstance(item, dict)
        }
        if env_selected:
            disk_payload["api_key"] = ""
        return disk_payload

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
        if not default_profile_id:
            default_profile_id = profiles[0]["id"]
        profiles, default_profile_id = self._merge_env_profiles(profiles, default_profile_id)
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
        disk_payload = self._strip_env_profiles_for_disk(payload)
        self.path.write_text(
            json.dumps(disk_payload, ensure_ascii=False, indent=2),
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
            raw = raw if isinstance(raw, dict) else {}
            profiles, default_profile_id = self._normalize_profiles(raw)
            payload["profiles"] = profiles
            payload["default_profile_id"] = default_profile_id
            payload["operation_mode"] = str(raw.get("operation_mode", payload["operation_mode"]))
            payload["freeze_failed_candidates"] = bool(
                raw.get("freeze_failed_candidates", payload["freeze_failed_candidates"])
            )
            payload["min_chapter_chars"] = self._normalize_min_chapter_chars(
                raw.get("min_chapter_chars", payload["min_chapter_chars"]),
                fallback=int(payload["min_chapter_chars"]),
            )
            payload["review_interval_chapters"] = self._normalize_review_interval(
                raw.get("review_interval_chapters", payload["review_interval_chapters"]),
                fallback=int(payload["review_interval_chapters"]),
            )
            payload["progression_mode"] = self._normalize_progression_mode(
                raw.get("progression_mode", payload["progression_mode"])
            )
            payload["auto_band_checkpoint"] = bool(
                raw.get("auto_band_checkpoint", payload["auto_band_checkpoint"])
            )
            payload["band_warn_action"] = self._normalize_band_warn_action(
                raw.get("band_warn_action", payload["band_warn_action"])
            )
            payload["manual_checkpoints_enabled"] = bool(
                raw.get("manual_checkpoints_enabled", payload["manual_checkpoints_enabled"])
            )
            payload["future_constraints_enabled"] = bool(
                raw.get("future_constraints_enabled", payload["future_constraints_enabled"])
            )
            payload["skill_runtime_enabled"] = bool(
                raw.get("skill_runtime_enabled", payload["skill_runtime_enabled"])
            )
            payload["skill_registry_path"] = (
                str(raw.get("skill_registry_path", payload["skill_registry_path"])).strip()
                or str(payload["skill_registry_path"])
            )
            payload["skill_strictness"] = normalize_skill_strictness(
                raw.get("skill_strictness", payload["skill_strictness"])
            )
            payload["enabled_skill_groups"] = self._normalize_string_list(
                raw.get("enabled_skill_groups", payload["enabled_skill_groups"])
            )
            payload["disabled_skill_ids"] = self._normalize_string_list(
                raw.get("disabled_skill_ids", payload["disabled_skill_ids"])
            )
            payload["writer_profile"] = self._normalize_writer_profile(
                raw.get("writer_profile", payload["writer_profile"])
            )
        profiles = [dict(item) for item in payload.get("profiles", []) if isinstance(item, dict)]
        default_profile_id = str(payload.get("default_profile_id", "")).strip()
        profiles, default_profile_id = self._merge_env_profiles(profiles, default_profile_id)
        payload["profiles"] = profiles
        payload["default_profile_id"] = default_profile_id
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
        min_chapter_chars: int | None = None,
        review_interval_chapters: int | None = None,
        progression_mode: str | None = None,
        auto_band_checkpoint: bool | None = None,
        band_warn_action: str | None = None,
        manual_checkpoints_enabled: bool | None = None,
        future_constraints_enabled: bool | None = None,
        skill_runtime_enabled: bool | None = None,
        skill_registry_path: str | None = None,
        skill_strictness: str | None = None,
        enabled_skill_groups: list[str] | None = None,
        disabled_skill_ids: list[str] | None = None,
        writer_profile: dict[str, object] | None = None,
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
            if min_chapter_chars is not None:
                payload["min_chapter_chars"] = self._normalize_min_chapter_chars(
                    min_chapter_chars,
                    fallback=int(self._defaults["min_chapter_chars"]),
                )
            if review_interval_chapters is not None:
                payload["review_interval_chapters"] = self._normalize_review_interval(
                    review_interval_chapters,
                    fallback=int(self._defaults["review_interval_chapters"]),
                )
            if progression_mode is not None:
                payload["progression_mode"] = self._normalize_progression_mode(progression_mode)
            if auto_band_checkpoint is not None:
                payload["auto_band_checkpoint"] = bool(auto_band_checkpoint)
            if band_warn_action is not None:
                payload["band_warn_action"] = self._normalize_band_warn_action(band_warn_action)
            if manual_checkpoints_enabled is not None:
                payload["manual_checkpoints_enabled"] = bool(manual_checkpoints_enabled)
            if future_constraints_enabled is not None:
                payload["future_constraints_enabled"] = bool(future_constraints_enabled)
            if skill_runtime_enabled is not None:
                payload["skill_runtime_enabled"] = bool(skill_runtime_enabled)
            if skill_registry_path is not None:
                payload["skill_registry_path"] = skill_registry_path.strip() or str(
                    self._defaults["skill_registry_path"]
                )
            if skill_strictness is not None:
                payload["skill_strictness"] = normalize_skill_strictness(skill_strictness)
            if enabled_skill_groups is not None:
                payload["enabled_skill_groups"] = self._normalize_string_list(enabled_skill_groups)
            if disabled_skill_ids is not None:
                payload["disabled_skill_ids"] = self._normalize_string_list(disabled_skill_ids)
            if writer_profile is not None:
                payload["writer_profile"] = self._normalize_writer_profile(writer_profile)
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
