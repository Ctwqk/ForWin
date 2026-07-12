from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .experience import RewardTag


class TropeTemplate(BaseModel):
    template_id: str
    display_name: str = ""
    category: RewardTag
    subcategory: str = ""
    market_tier: Literal["sinking", "mainstream", "premium"] = "mainstream"
    cost_weight: int = 2
    genre_fit: list[str] = Field(default_factory=list)
    audience_fit: list[str] = Field(default_factory=list)
    platform_fit: list[str] = Field(default_factory=list)
    setup_requirement: str = ""
    payoff_shape: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    best_window: str = ""
    recommended_hook_types: list[str] = Field(default_factory=list)
    pressure_shape: str = ""
    protagonist_action: str = ""
    visible_payoff: str = ""
    audience_reaction: str = ""
    next_hook_shape: str = ""
    anti_patterns: list[str] = Field(default_factory=list)
    review_signals: list[str] = Field(default_factory=list)
    desire_setup: str = ""
    resistance: str = ""
    payoff: str = ""
    aftermath: str = ""


REQUIRED_REWARD_CATEGORIES = {"power", "social", "justice", "mystery", "emotion"}
MINIMUM_USABLE_LIBRARY_COUNT = 50
FULL_LIBRARY_EXPECTED_COUNT = 188

_CATEGORY_DEFAULTS: dict[str, dict[str, object]] = {
    "power": {
        "genre_fit": ["玄幻", "都市", "神豪", "末世", "种田", "职场"],
        "audience_fit": ["下沉市场", "番茄免费阅读", "成长爽文读者"],
        "platform_fit": ["fanqie", "webnovel_cn", "mobile_free"],
        "payoff_shape": "给主角一个可验证的能力、资源或状态进展，并立刻改变当前局面。",
        "visible_payoff": "能力变化、资源到账、权限打开或战局逆转必须被旁观者和客观结果确认。",
        "pressure_shape": "用公开质疑、限时危机或资源压迫逼主角行动。",
        "protagonist_action": "主角主动调用前文积累，完成一次短链路验证。",
        "audience_reaction": "读者应看到限制被突破、对手失态、下一层门槛出现。",
        "next_hook_shape": "更高层敌人、代价、副作用或新资源缺口出现。",
    },
    "social": {
        "genre_fit": ["都市", "神豪", "赘婿", "职场", "宫斗", "年代", "玄幻"],
        "audience_fit": ["下沉市场", "番茄免费阅读", "地位逆转读者"],
        "platform_fit": ["fanqie", "webnovel_cn", "mobile_free"],
        "payoff_shape": "让公开评价、权力排序或关系称呼发生可见变化。",
        "visible_payoff": "座位、称呼、合同、资格、背书或旁观者态度必须当场变化。",
        "pressure_shape": "用公开羞辱、规则卡位或群体误判制造地位压力。",
        "protagonist_action": "主角用证据、身份、成绩或选择权完成反转。",
        "audience_reaction": "读者应看到轻视者失语、旁观者改口、主角掌握选择权。",
        "next_hook_shape": "反转引来报复、更高层注意或新的社交债。",
    },
    "justice": {
        "genre_fit": ["都市", "玄幻", "末世", "年代", "宫斗", "种田"],
        "audience_fit": ["下沉市场", "番茄免费阅读", "清算爽点读者"],
        "platform_fit": ["fanqie", "webnovel_cn", "mobile_free"],
        "payoff_shape": "让恶行、证据、惩罚和损失落到具体人和具体后果。",
        "visible_payoff": "押走、罚没、除名、赔偿、收回利益或公开道歉必须落地。",
        "pressure_shape": "让反派短暂得意，展示庇护伞、假证据或规则漏洞。",
        "protagonist_action": "主角主动推动机制、证据链或规则清算。",
        "audience_reaction": "读者应看到恶人失去继续作恶的能力。",
        "next_hook_shape": "背后势力、受害者安置或新清算对象浮出水面。",
    },
    "mystery": {
        "genre_fit": ["都市", "玄幻", "末世", "宫斗", "年代", "悬疑"],
        "audience_fit": ["下沉市场", "番茄免费阅读", "悬念推进读者"],
        "platform_fit": ["fanqie", "webnovel_cn", "mobile_free"],
        "payoff_shape": "回收一个旧问题的一部分，同时打开更高价值的新问题。",
        "visible_payoff": "线索、名字、地点、记录、符号或矛盾证据必须具体可见。",
        "pressure_shape": "用时间限制、信息缺口、被破坏的现场或沉默证人施压。",
        "protagonist_action": "主角通过观察、推理、交易或冒险取得线索。",
        "audience_reaction": "读者应感到不是被吊着，而是拿到了可追踪的新证据。",
        "next_hook_shape": "线索指向下一站、下一人或更大的黑幕。",
    },
    "emotion": {
        "genre_fit": ["都市", "赘婿", "萌宝", "宫斗", "年代", "末世", "玄幻"],
        "audience_fit": ["下沉市场", "番茄免费阅读", "关系黏性读者"],
        "platform_fit": ["fanqie", "webnovel_cn", "mobile_free"],
        "payoff_shape": "让关系站位、保护动作、愧疚或信任发生可观察变化。",
        "visible_payoff": "称呼、距离、眼神、站队、承担风险或第一次主动靠近必须出现。",
        "pressure_shape": "用当下危险、误解、亏欠或身份压力逼出情感选择。",
        "protagonist_action": "主角以行动承担风险，而不是只口头承诺。",
        "audience_reaction": "读者应看到人物关系推进一小步并留下记忆点。",
        "next_hook_shape": "关系变化带来误会、报复、身世问题或新的保护代价。",
    },
}

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
    return expand_trope_template_payload([item for item in payload if isinstance(item, dict)])


def expand_trope_template_payload(
    payload: list[dict],
    *,
    minimum_count: int = MINIMUM_USABLE_LIBRARY_COUNT,
) -> list[dict]:
    _ = minimum_count
    expanded: list[dict] = []
    seen_ids: set[str] = set()
    for item in payload:
        enriched = _enrich_template_payload(item)
        template_id = str(enriched.get("template_id") or "").strip()
        if not template_id or template_id in seen_ids:
            continue
        expanded.append(enriched)
        seen_ids.add(template_id)

    return expanded


def _enrich_template_payload(item: dict) -> dict:
    payload = dict(item)
    category = str(payload.get("category") or "mystery").strip()
    defaults = _CATEGORY_DEFAULTS.get(category, _CATEGORY_DEFAULTS["mystery"])
    display_name = str(payload.get("display_name") or payload.get("template_id") or "爽点").strip()
    subcategory = str(payload.get("subcategory") or display_name).strip()
    cost_weight = int(payload.get("cost_weight") or 2)
    payload.setdefault("subcategory", subcategory)
    payload.setdefault("market_tier", "sinking" if cost_weight <= 2 else "mainstream")
    for field_name in ("genre_fit", "audience_fit", "platform_fit"):
        if not payload.get(field_name):
            payload[field_name] = list(defaults.get(field_name, []))
    for field_name in (
        "payoff_shape",
        "visible_payoff",
        "pressure_shape",
        "protagonist_action",
        "audience_reaction",
        "next_hook_shape",
    ):
        if not payload.get(field_name):
            payload[field_name] = str(defaults.get(field_name, ""))
    if not payload.get("setup_requirement"):
        payload["setup_requirement"] = f"先用低成本场景建立{subcategory}的当前需求和阻力。"
    if not payload.get("desire_setup"):
        payload["desire_setup"] = (
            f"写清主角为什么需要{subcategory}：当前限制、公开压力或读者期待必须具体可见。"
        )
    if not payload.get("resistance"):
        payload["resistance"] = str(defaults.get("pressure_shape") or "")
    if not payload.get("payoff"):
        payload["payoff"] = str(defaults.get("visible_payoff") or defaults.get("payoff_shape") or "")
    if not payload.get("aftermath"):
        payload["aftermath"] = (
            f"兑现{subcategory}后，写出旁观者/对手/主角处境的变化，并接出下一章钩子。"
        )
    if not payload.get("anti_patterns"):
        payload["anti_patterns"] = [
            "只给关键词不写动作链",
            "没有客观证据证明爽点已兑现",
            "一次解决整条主线",
        ]
    if not payload.get("review_signals"):
        payload["review_signals"] = [
            "是否有明确欲望、阻力、兑现、余波四段？",
            "兑现是否可被动作、物件、制度或旁观者反应验证？",
            "章末是否留下新的行动钩子？",
        ]
    return payload


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


def _default_markdown_library_path() -> Path:
    return Path(__file__).resolve().parents[2] / "Design-docs" / "trope_library_pulp_v1.md"


@lru_cache(maxsize=1)
def load_trope_template_library() -> tuple[TropeTemplate, ...]:
    override_path = os.environ.get("FORWIN_TROPE_TEMPLATE_PATH", "").strip()
    if override_path:
        path = Path(override_path)
        if path.suffix.lower() == ".md":
            from .trope_md_loader import load_trope_templates_from_md

            return load_trope_templates_from_md(path)
        return load_trope_template_file(path, require_full=True)
    markdown_path = _default_markdown_library_path()
    if markdown_path.exists():
        from .trope_md_loader import load_trope_templates_from_md

        return load_trope_templates_from_md(markdown_path)
    seed_templates, seed_errors = validate_trope_template_payload(_seed_payload())
    if seed_errors:
        raise ValueError("; ".join(seed_errors))
    return seed_templates


TROPE_TEMPLATE_LIBRARY = load_trope_template_library()


def trope_registry_summary() -> TropeRegistrySummary:
    override_path = os.environ.get("FORWIN_TROPE_TEMPLATE_PATH", "").strip()
    default_markdown_path = _default_markdown_library_path()
    source = override_path or (str(default_markdown_path) if default_markdown_path.exists() else "seed")
    validation_errors: list[str] = []
    version = "starter"
    try:
        templates = load_trope_template_library()
    except Exception as exc:  # noqa: BLE001
        templates = ()
        validation_errors = [str(exc)]
    if not validation_errors and len(templates) == FULL_LIBRARY_EXPECTED_COUNT:
        version = "full"
    elif not validation_errors and len(templates) >= MINIMUM_USABLE_LIBRARY_COUNT:
        version = "expanded"
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
    return [item for item in load_trope_template_library() if item.category == category]


def trope_template_index() -> dict[str, TropeTemplate]:
    return {item.template_id: item for item in load_trope_template_library()}
