from __future__ import annotations

from dataclasses import dataclass, field

from forwin.experience.trope_cooldown import (
    TropeCooldownPolicy,
    overused_template_ids,
    select_available_templates,
)
from forwin.protocol.trope_library import TropeTemplate


@dataclass(frozen=True)
class TropeSelectionContext:
    category: str = ""
    genre: str = ""
    audience_fit: list[str] = field(default_factory=list)
    platform: str = "fanqie"
    cost_ceiling: int = 3
    recent_template_ids: list[str] = field(default_factory=list)
    recent_categories: list[str] = field(default_factory=list)
    blocked_template_ids: set[str] = field(default_factory=set)
    blocked_categories: set[str] = field(default_factory=set)
    used_template_ids: set[str] = field(default_factory=set)
    policy: TropeCooldownPolicy = field(default_factory=TropeCooldownPolicy)


class TropeSelector:
    def select_template(
        self,
        *,
        category_templates: list[TropeTemplate],
        library_templates: list[TropeTemplate],
        context: TropeSelectionContext,
        preferred_template_id: str = "",
    ) -> TropeTemplate | None:
        category_sorted = _sorted_templates(category_templates)
        library_sorted = _sorted_templates(library_templates)
        preferred = self._preferred_template(
            template_id=preferred_template_id,
            templates=[*category_sorted, *library_sorted],
            context=context,
        )
        if preferred is not None:
            return preferred

        under_ceiling = [
            template
            for template in category_sorted
            if int(template.cost_weight or 0) <= _normalize_cost_ceiling(context.cost_ceiling)
            and _template_allowed(template, context=context, require_fit=True)
        ]
        under_ceiling = select_available_templates(
            under_ceiling,
            recent_template_ids=context.recent_template_ids,
            recent_categories=context.recent_categories,
            policy=context.policy,
        )
        fallback_same_category = [
            template
            for template in category_sorted
            if _template_allowed(template, context=context, require_fit=True)
        ]
        fallback_library = [
            template
            for template in library_sorted
            if _template_allowed(template, context=context, require_fit=True)
        ]
        for candidates in (under_ceiling, fallback_same_category, fallback_library):
            if candidates:
                return candidates[0]
        return None

    def _preferred_template(
        self,
        *,
        template_id: str,
        templates: list[TropeTemplate],
        context: TropeSelectionContext,
    ) -> TropeTemplate | None:
        normalized = str(template_id or "").strip()
        if not normalized:
            return None
        for template in templates:
            if str(template.template_id or "").strip() != normalized:
                continue
            if _template_allowed(template, context=context, require_fit=False):
                return template
        return None


def _template_allowed(
    template: TropeTemplate,
    *,
    context: TropeSelectionContext,
    require_fit: bool,
) -> bool:
    template_id = str(template.template_id or "").strip()
    category = str(template.category or "").strip()
    blocked_template_ids = {str(item).strip() for item in context.blocked_template_ids if str(item).strip()}
    blocked_categories = {str(item).strip() for item in context.blocked_categories if str(item).strip()}
    used_template_ids = {str(item).strip() for item in context.used_template_ids if str(item).strip()}
    overused_templates = overused_template_ids(context.recent_template_ids, policy=context.policy)
    if (
        not template_id
        or template_id in used_template_ids
        or template_id in blocked_template_ids
        or template_id in overused_templates
        or category in blocked_categories
    ):
        return False
    return not require_fit or _template_fits_context(template, context)


def _template_fits_context(template: TropeTemplate, context: TropeSelectionContext) -> bool:
    genre = str(context.genre or "").strip()
    if genre and template.genre_fit and not _any_text_overlap(genre, template.genre_fit):
        return False
    platform = str(context.platform or "").strip()
    if platform and template.platform_fit and platform not in {str(item).strip() for item in template.platform_fit}:
        return False
    audience = [str(item).strip() for item in context.audience_fit if str(item).strip()]
    if audience and template.audience_fit:
        allowed = {str(item).strip() for item in template.audience_fit if str(item).strip()}
        if not any(item in allowed for item in audience):
            return False
    return True


def _any_text_overlap(value: str, candidates: list[str]) -> bool:
    return any(str(item).strip() and (str(item).strip() in value or value in str(item).strip()) for item in candidates)


def _normalize_cost_ceiling(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 3


def _sorted_templates(templates: object) -> list[TropeTemplate]:
    return sorted(
        list(templates or []),
        key=lambda template: (int(getattr(template, "cost_weight", 2) or 0), str(template.template_id)),
    )


__all__ = ["TropeSelectionContext", "TropeSelector"]
