from __future__ import annotations

from typing import Any


def _legacy():
    from forwin import book_genesis as legacy

    return legacy


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
        legacy = _legacy()
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in legacy.GENESIS_STAGE_ORDER:
            raise ValueError("未知 Genesis stage。")
        pack = self.owner.load_pack(revision)
        if isinstance(stage_payload_override, dict):
            pack = dict(pack)
            legacy._set_pack_stage_payload(pack, normalized_stage, stage_payload_override)
        stage_payload = legacy._pack_stage_payload(pack, normalized_stage)
        normalized_target = str(target_path or "").strip()
        normalized_field = str(field_path or "").strip()
        if not normalized_field:
            raise ValueError("field_path 不能为空。")
        resolved_kind = str(kind or "").strip() or legacy._infer_name_kind(
            stage_key=normalized_stage,
            target_path=normalized_target,
            field_path=normalized_field,
        )
        if resolved_kind not in {"person", "region", "place", "epithet"}:
            raise ValueError("无法推断命名类型，请显式提供 kind。")
        culture_profile = self.owner._resolve_name_generation_profile(
            stage_key=normalized_stage,
            pack=pack,
            stage_payload=stage_payload,
            target_path=normalized_target,
        )
        civilization = legacy._culture_profile_generator_civilization(culture_profile)
        if not civilization:
            raise ValueError("当前对象没有可用的文化背景命名配置。")
        normalized_count = max(1, min(int(count or 1), 12))
        try:
            suggestions = legacy._generate_culture_names(
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
        if not legacy._field_expects_list(normalized_field) and normalized_count == 1:
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

