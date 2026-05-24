from __future__ import annotations

from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _tags(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_clean(item) for item in values if _clean(item)]


class PlatformMetadataCatalog:
    QIDIAN_AUDIENCE = {
        "male": {"value": "male", "label": "男生"},
        "男": {"value": "male", "label": "男生"},
        "男频": {"value": "male", "label": "男生"},
        "男生": {"value": "male", "label": "男生"},
        "female": {"value": "female", "label": "女生"},
        "女": {"value": "female", "label": "女生"},
        "女频": {"value": "female", "label": "女生"},
        "女生": {"value": "female", "label": "女生"},
    }
    FANQIE_AUDIENCE = {
        "male": {"value": 1, "label": "男频"},
        "男": {"value": 1, "label": "男频"},
        "男频": {"value": 1, "label": "男频"},
        "男生": {"value": 1, "label": "男频"},
        "female": {"value": 0, "label": "女频"},
        "女": {"value": 0, "label": "女频"},
        "女频": {"value": 0, "label": "女频"},
        "女生": {"value": 0, "label": "女频"},
    }
    QIDIAN_CATEGORY = {
        "玄幻": {"label": "玄幻", "subcategory": "东方玄幻"},
        "传统玄幻": {"label": "玄幻", "subcategory": "东方玄幻"},
        "都市": {"label": "都市", "subcategory": "都市生活"},
        "都市日常": {"label": "都市", "subcategory": "都市生活"},
        "历史": {"label": "历史", "subcategory": "架空历史"},
        "仙侠": {"label": "仙侠", "subcategory": "幻想修仙"},
        "科幻": {"label": "科幻", "subcategory": "未来世界"},
        "悬疑": {"label": "悬疑", "subcategory": "侦探推理"},
        "悬疑灵异": {"label": "悬疑", "subcategory": "诡秘悬疑"},
        "奇幻": {"label": "奇幻", "subcategory": "现代魔法"},
        "游戏": {"label": "游戏", "subcategory": "游戏异界"},
        "武侠": {"label": "武侠", "subcategory": "武侠幻想"},
        "现实": {"label": "现实", "subcategory": "现实百态"},
        "古代言情": {"label": "古代言情", "subcategory": "古典架空"},
        "现代言情": {"label": "现代言情", "subcategory": "都市生活"},
    }
    FANQIE_CATEGORY = {
        "都市": {"label": "都市", "subcategory": "都市生活"},
        "都市日常": {"label": "都市", "subcategory": "都市生活"},
        "玄幻": {"label": "玄幻", "subcategory": "传统玄幻"},
        "传统玄幻": {"label": "玄幻", "subcategory": "传统玄幻"},
        "悬疑": {"label": "悬疑灵异", "subcategory": "悬疑灵异"},
        "悬疑灵异": {"label": "悬疑灵异", "subcategory": "悬疑灵异"},
        "仙侠": {"label": "奇幻仙侠", "subcategory": "古典仙侠"},
        "奇幻": {"label": "奇幻仙侠", "subcategory": "西方奇幻"},
        "历史": {"label": "历史", "subcategory": "历史古代"},
        "科幻": {"label": "科幻", "subcategory": "未来科幻"},
        "游戏": {"label": "游戏体育", "subcategory": "游戏异界"},
        "现代言情": {"label": "现言脑洞", "subcategory": "现代言情"},
        "古代言情": {"label": "宫斗宅斗", "subcategory": "古代言情"},
    }
    FANQIE_THEME_TAGS = {
        "都市日常": "都市生活",
        "都市生活": "都市生活",
        "传统玄幻": "传统玄幻",
        "悬疑": "悬疑灵异",
        "悬疑灵异": "悬疑灵异",
        "修仙": "玄幻仙侠",
    }
    FANQIE_ROLE_TAGS = {
        "系统": "系统流",
        "系统流": "系统流",
        "天才": "天才流",
        "凡人": "凡人流",
        "群像": "群像",
    }
    FANQIE_PLOT_TAGS = {
        "悬疑": "悬疑灵异",
        "破案": "悬疑灵异",
        "爽文": "爽文",
        "升级": "升级流",
        "经营": "经营",
    }

    def resolve_for_platform(self, platform_id: str, book_meta: dict[str, Any] | None) -> dict[str, Any]:
        platform = _clean(platform_id)
        meta = book_meta if isinstance(book_meta, dict) else {}
        warnings: list[dict[str, str]] = []
        if platform == "fanqie":
            audience = self._resolve_fanqie_audience(meta, warnings)
            category = self._resolve_category(
                meta,
                mapping=self.FANQIE_CATEGORY,
                fallback={"label": "都市", "subcategory": "都市生活"},
                warnings=warnings,
            )
            return {
                "platform": platform,
                "resolved_audience": audience,
                "resolved_primary_category": {"label": category["label"]},
                "resolved_subcategory": category.get("subcategory", ""),
                "resolved_theme_tags": self._map_tags(meta.get("theme_tags"), self.FANQIE_THEME_TAGS),
                "resolved_role_tags": self._map_tags(meta.get("role_tags"), self.FANQIE_ROLE_TAGS),
                "resolved_plot_tags": self._map_tags(meta.get("plot_tags"), self.FANQIE_PLOT_TAGS),
                "required_fields": ["book_name", "intro", "protagonist_names", "audience", "primary_category"],
                "warnings": warnings,
            }
        audience = self._resolve_qidian_audience(meta, warnings)
        category = self._resolve_category(
            meta,
            mapping=self.QIDIAN_CATEGORY,
            fallback={"label": "都市", "subcategory": "都市生活"},
            warnings=warnings,
        )
        return {
            "platform": platform,
            "resolved_audience": audience,
            "resolved_primary_category": {"label": category["label"]},
            "resolved_subcategory": category.get("subcategory", ""),
            "resolved_theme_tags": [],
            "resolved_role_tags": [],
            "resolved_plot_tags": [],
            "required_fields": ["book_name", "intro", "audience", "primary_category"],
            "warnings": warnings,
        }

    def _resolve_qidian_audience(
        self,
        meta: dict[str, Any],
        warnings: list[dict[str, str]],
    ) -> dict[str, Any]:
        raw = _clean(meta.get("audience"))
        if raw in self.QIDIAN_AUDIENCE:
            return dict(self.QIDIAN_AUDIENCE[raw])
        if raw:
            warnings.append(
                {
                    "code": "audience_fallback",
                    "message": f"起点受众 {raw} 未映射，默认男生。",
                }
            )
        return dict(self.QIDIAN_AUDIENCE["male"])

    def _resolve_fanqie_audience(
        self,
        meta: dict[str, Any],
        warnings: list[dict[str, str]],
    ) -> dict[str, Any]:
        raw = _clean(meta.get("audience"))
        if raw in self.FANQIE_AUDIENCE:
            return dict(self.FANQIE_AUDIENCE[raw])
        if raw:
            warnings.append(
                {
                    "code": "audience_fallback",
                    "message": f"番茄频道 {raw} 未映射，默认男频。",
                }
            )
        return dict(self.FANQIE_AUDIENCE["male"])

    def _resolve_category(
        self,
        meta: dict[str, Any],
        *,
        mapping: dict[str, dict[str, str]],
        fallback: dict[str, str],
        warnings: list[dict[str, str]],
    ) -> dict[str, str]:
        candidates = [_clean(meta.get("primary_category"))]
        candidates.extend(_tags(meta.get("theme_tags")))
        for candidate in candidates:
            if candidate in mapping:
                return dict(mapping[candidate])
        if any(candidates):
            warnings.append(
                {
                    "code": "category_fallback",
                    "message": f"分类 {next(item for item in candidates if item)} 未映射，使用默认分类 {fallback['label']}。",
                }
            )
        return dict(fallback)

    def _map_tags(self, values: Any, mapping: dict[str, str]) -> list[str]:
        resolved: list[str] = []
        for tag in _tags(values):
            mapped = mapping.get(tag)
            if mapped and mapped not in resolved:
                resolved.append(mapped)
        return resolved[:3]
