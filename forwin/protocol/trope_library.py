from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, Field

from .experience import RewardTag


class TropeTemplate(BaseModel):
    template_id: str
    display_name: str = ""
    category: RewardTag
    setup_requirement: str = ""
    payoff_shape: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    best_window: str = ""
    recommended_hook_types: list[str] = Field(default_factory=list)


REQUIRED_REWARD_CATEGORIES = {"power", "social", "justice", "mystery", "emotion"}
FULL_LIBRARY_EXPECTED_COUNT = 188


class TropeRegistrySummary(BaseModel):
    total_count: int = 0
    category_counts: dict[str, int] = Field(default_factory=dict)
    version: str = "starter"
    source: str = "seed"
    is_full_library: bool = False
    validation_errors: list[str] = Field(default_factory=list)


def _seed_payload() -> list[dict]:
    seed_text = resources.files("forwin.protocol").joinpath("trope_templates.seed.json").read_text(
        encoding="utf-8"
    )
    payload = json.loads(seed_text)
    if not isinstance(payload, list):
        raise ValueError("trope_templates.seed.json must contain a list")
    return [item for item in payload if isinstance(item, dict)]


def validate_trope_template_payload(
    payload: object,
    *,
    require_full: bool = False,
) -> tuple[tuple[TropeTemplate, ...], list[str]]:
    errors: list[str] = []
    if not isinstance(payload, list):
        return (), ["trope template payload must be a list"]
    templates: list[TropeTemplate] = []
    seen_ids: set[str] = set()
    category_counts: dict[str, int] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            errors.append(f"item[{index}] must be an object")
            continue
        try:
            template = TropeTemplate.model_validate(item)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"item[{index}] invalid: {exc}")
            continue
        template_id = str(template.template_id or "").strip()
        if not template_id:
            errors.append(f"item[{index}] template_id is required")
            continue
        if template_id in seen_ids:
            errors.append(f"duplicate template_id: {template_id}")
            continue
        seen_ids.add(template_id)
        category_counts[str(template.category)] = category_counts.get(str(template.category), 0) + 1
        templates.append(template)

    missing_categories = sorted(REQUIRED_REWARD_CATEGORIES - set(category_counts))
    if missing_categories:
        errors.append(f"missing categories: {', '.join(missing_categories)}")
    if require_full and len(templates) != FULL_LIBRARY_EXPECTED_COUNT:
        errors.append(
            f"full trope library must contain exactly {FULL_LIBRARY_EXPECTED_COUNT} templates, got {len(templates)}"
        )
    return tuple(templates), errors


def load_trope_template_file(path: str | os.PathLike[str], *, require_full: bool = True) -> tuple[TropeTemplate, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    templates, errors = validate_trope_template_payload(payload, require_full=require_full)
    if errors:
        raise ValueError("; ".join(errors))
    return templates


@lru_cache(maxsize=1)
def load_trope_template_library() -> tuple[TropeTemplate, ...]:
    seed_templates, seed_errors = validate_trope_template_payload(_seed_payload())
    if seed_errors:
        raise ValueError("; ".join(seed_errors))
    override_path = os.environ.get("FORWIN_TROPE_TEMPLATE_PATH", "").strip()
    if override_path:
        try:
            return load_trope_template_file(override_path, require_full=True)
        except Exception:
            return seed_templates
    return seed_templates


TROPE_TEMPLATE_LIBRARY = load_trope_template_library()


def trope_registry_summary() -> TropeRegistrySummary:
    override_path = os.environ.get("FORWIN_TROPE_TEMPLATE_PATH", "").strip()
    source = override_path or "seed"
    validation_errors: list[str] = []
    version = "starter"
    templates = TROPE_TEMPLATE_LIBRARY
    if override_path:
        try:
            payload = json.loads(Path(override_path).read_text(encoding="utf-8"))
            _, validation_errors = validate_trope_template_payload(payload, require_full=True)
            if not validation_errors and len(templates) == FULL_LIBRARY_EXPECTED_COUNT:
                version = "full"
        except Exception as exc:  # noqa: BLE001
            validation_errors = [str(exc)]
    category_counts: dict[str, int] = {}
    for template in templates:
        category_counts[str(template.category)] = category_counts.get(str(template.category), 0) + 1
    return TropeRegistrySummary(
        total_count=len(templates),
        category_counts=category_counts,
        version=version,
        source=source,
        is_full_library=version == "full" and len(templates) == FULL_LIBRARY_EXPECTED_COUNT,
        validation_errors=validation_errors,
    )


def trope_templates_by_category(category: RewardTag) -> list[TropeTemplate]:
    return [item for item in TROPE_TEMPLATE_LIBRARY if item.category == category]


def trope_template_index() -> dict[str, TropeTemplate]:
    return {item.template_id: item for item in TROPE_TEMPLATE_LIBRARY}
