from __future__ import annotations

from typing import Any

from forwin.genesis.constants import GENESIS_STAGE_ORDER
from forwin.genesis.fallbacks import _fallback_culture_profiles
from forwin.genesis.helpers import (
    _normalize_stage_target_path,
    _pack_stage_payload,
    _set_pack_stage_payload,
)
from forwin.genesis.names_paths import (
    _culture_profile_generator_civilization,
    _field_expects_list,
    _generate_culture_names,
    _get_value_at_path,
    _infer_name_kind,
)


class GenesisNameSuggestionService:
    """Generates culture-aware name candidates without mutating the Genesis pack."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def generate_name_suggestions(
        self,
        *,
        project: Any,
        revision: Any,
        stage_key: str,
        target_path: str,
        field_path: str,
        kind: str = "",
        count: int = 1,
        nonce: str = "",
        stage_payload_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise ValueError("未知 Genesis stage。")
        pack = self.owner.load_pack(revision)
        if isinstance(stage_payload_override, dict):
            pack = dict(pack)
            _set_pack_stage_payload(pack, normalized_stage, stage_payload_override)
        stage_payload = _pack_stage_payload(pack, normalized_stage)
        normalized_target = str(target_path or "").strip()
        normalized_field = str(field_path or "").strip()
        if not normalized_field:
            raise ValueError("field_path 不能为空。")
        resolved_kind = str(kind or "").strip() or _infer_name_kind(
            stage_key=normalized_stage,
            target_path=normalized_target,
            field_path=normalized_field,
        )
        if resolved_kind not in {"person", "region", "place", "epithet"}:
            raise ValueError("无法推断命名类型，请显式提供 kind。")
        culture_profile = self.resolve_name_generation_profile(
            project=project,
            stage_key=normalized_stage,
            pack=pack,
            stage_payload=stage_payload,
            target_path=normalized_target,
        )
        civilization = _culture_profile_generator_civilization(culture_profile)
        if not civilization:
            raise ValueError("当前对象没有可用的文化背景命名配置。")
        normalized_count = max(1, min(int(count or 1), 12))
        try:
            suggestions = _generate_culture_names(
                civilization=civilization,
                kind=resolved_kind,
                count=normalized_count,
                seed=":".join(
                    [
                        str(project.id or ""),
                        str(getattr(revision, "id", "") or ""),
                        normalized_stage,
                        normalized_target,
                        normalized_field,
                        resolved_kind,
                        str(culture_profile.get("id", "") or ""),
                        str(nonce or ""),
                    ]
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"名称生成失败：{exc}") from exc
        applied_value: Any = suggestions
        if not _field_expects_list(normalized_field) and normalized_count == 1:
            applied_value = suggestions[0]
        return {
            "ok": True,
            "stage_key": normalized_stage,
            "target_path": normalized_target,
            "field_path": normalized_field,
            "kind": resolved_kind,
            "suggestions": suggestions,
            "applied_value": applied_value,
            "culture_profile_id": str(culture_profile.get("id", "")).strip(),
            "culture_profile_name": str(culture_profile.get("name", "")).strip(),
            "generator_civilization": civilization,
            "message": "已根据文化背景生成名称建议。",
        }

    @staticmethod
    def resolve_name_generation_profile(
        *,
        project: Any,
        stage_key: str,
        pack: dict[str, Any],
        stage_payload: dict[str, Any],
        target_path: str,
    ) -> dict[str, Any]:
        normalized_target_path = _normalize_stage_target_path(
            stage_key,
            target_path,
        )
        world_root = _pack_stage_payload(pack, "world")
        world_bible = (
            world_root.get("world_bible")
            if isinstance(world_root.get("world_bible"), dict)
            else {}
        )
        profiles = [
            item
            for item in (world_bible.get("culture_profiles") or [])
            if isinstance(item, dict)
        ]
        profile_by_id = {
            str(item.get("id", "")).strip(): item
            for item in profiles
            if str(item.get("id", "")).strip()
        }
        if stage_key == "world" and normalized_target_path.startswith(
            "world_bible.culture_profiles["
        ):
            target = _get_value_at_path(
                stage_payload,
                normalized_target_path,
            )
            if isinstance(target, dict):
                return target
        target_value = None
        if normalized_target_path:
            try:
                target_value = _get_value_at_path(
                    stage_payload,
                    normalized_target_path,
                )
            except ValueError:
                target_value = None
        if isinstance(target_value, dict):
            culture_profile_id = str(
                target_value.get("culture_profile_id", "")
            ).strip()
            if culture_profile_id and culture_profile_id in profile_by_id:
                return profile_by_id[culture_profile_id]
        if profiles:
            return profiles[0]
        return _fallback_culture_profiles(project, pack)[0]
