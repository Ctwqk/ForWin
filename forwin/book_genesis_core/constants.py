from __future__ import annotations

import copy
import inspect
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.arc_sizing import allocate_arc_chapter_sizes
from forwin.governance import DecisionEventInfo, DecisionEventType, normalize_project_governance
from forwin.genesis_handoff import GenesisHandoffService
from forwin.genesis_workspace import GenesisWorkspaceService
from forwin.genesis_workspace.trace_service import GenesisTraceService
from forwin.map.service import ensure_book_map_from_genesis_atlas
from forwin.model_adapter import ModelAdapter
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.observability.context import OperationContext
from forwin.observability.payloads import audit_payload, event_error_payload
from forwin.observability.ports import NullObservability
from forwin.observability.redaction import redact_payload
from forwin.observability.spans import SpanRecord
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.naming import CULTURE_ALIAS_TO_KEY, CULTURES, CultureNameGenerator
from forwin.observability.llm_trace import (
    build_llm_decision_event_payloads,
    mark_latest_attempt_parse_failure,
    prepare_prompt_trace_payload,
)
from forwin.skills import (
    SkillPromptLayerBuilder,
    SkillRouter,
    inject_skill_layers,
    serialize_prompt_layers,
    summarize_selected_skills,
)
from forwin.state.updater import StateUpdater
from forwin.utils import LLMJSONParseError, parse_llm_json
from forwin.world_templates import (
    default_minimum_extension_pack,
    default_minimum_world_system,
    default_template_libraries,
    default_world_extensions,
    empty_world_root,
)
from forwin.writer.llm_client import LLMClient

logger = logging.getLogger(__name__)

GENESIS_STAGE_ORDER = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)
_STAGE_TO_SECTION = {
    "brief": "book_brief",
    "world": "world",
    "map": "world.map_atlas",
    "story_engine": "world.story_engine",
    "book_blueprint": "book_arc_blueprint",
    "bootstrap": "execution_bootstrap",
}
_PATH_TOKEN_RE = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")
_WORLD_ROOT_KEYS = {
    "minimum_world_system",
    "minimum_extension_pack",
    "world_bible",
    "map_atlas",
    "story_engine",
    "institution_profiles",
    "resource_economy_profiles",
    "world_extensions",
    "template_libraries",
}
_WORLD_BIBLE_KEYS = {
    "overview",
    "axioms",
    "history_slice",
    "naming_style",
    "forbidden_zones",
    "culture_profiles",
}
_WORLD_STAGE_RELATIVE_PREFIXES = {
    "minimum_world_system",
    "minimum_extension_pack",
    "world_bible",
    "map_atlas",
    "story_engine",
    "institution_profiles",
    "resource_economy_profiles",
    "world_extensions",
    "template_libraries",
}
_WORLD_STAGE_WORLD_BIBLE_ALIASES = {
    "overview",
    "axioms",
    "history_slice",
    "naming_style",
    "forbidden_zones",
    "culture_profiles",
}
_GENESIS_STAGE_LABELS = {
    "brief": "创意简报",
    "world": "世界观与背景",
    "map": "地图与空间拓扑",
    "story_engine": "角色势力与叙事引擎",
    "book_blueprint": "整本书多 Arc 路线图",
    "bootstrap": "执行契约与启动交接",
}
_WORLD_STAGE_STATE_KEYS = (
    "minimum_world_system",
    "minimum_extension_pack",
    "world_bible",
    "institution_profiles",
    "resource_economy_profiles",
    "world_extensions",
    "template_libraries",
)
_GENESIS_SYSTEM_FOUNDATION = (
    "你是中文长篇网文的 Genesis 总设计师。你的目标是产出可直接进入下一阶段和后续写作流程的结构化蓝图，而不是写解释性文案。\n"
    "必须只输出一个 JSON 对象，不要 markdown、代码块、注释、额外说明。\n"
    "优先保证：长期可连载、冲突可持续升级、设定与人物/势力/空间相互咬合、字段能被后续步骤直接复用。\n"
    "若消息里提供“已锁定阶段上下文”，将其视为当前真值和硬约束，后续输出必须与之兼容，不能推翻已锁定结论。\n"
    "若上游已有稳定 id、命名体系、文化背景、地区/势力关系，除非用户明确要求或存在明显冲突，不要随意改名、换 id、重排结构。\n"
    "信息不足时补最小可运行骨架，避免 null、空洞套话、只有标题没有可执行内容。"
)
_GENESIS_STAGE_SYSTEM_PROMPTS = {
    "brief": (
        "当前阶段负责把新书 premise 压缩成整本书承诺。重点是卖点、目标读者、核心情绪、核心爽点、长期 promise 和内容 guardrails，"
        "让后续世界观、叙事引擎和 Arc 蓝图都能直接复用。"
    ),
    "world": (
        "当前阶段负责搭建 WorldRoot。重点是能支撑长篇升级的规则、历史切片、文化模板与命名体系；"
        "world_bible 要具体，minimum_world_system / minimum_extension_pack 要能落地，其他根层结构要保持可扩展。"
    ),
    "map": (
        "当前阶段负责搭建 MapAtlas。重点是空间层级、移动成本、权力覆盖、危险区与资源分布，让地图天然服务剧情推进，"
        "而不是只列一串地名。"
    ),
    "story_engine": (
        "当前阶段负责搭建 StoryEngine。重点是角色欲望与恐惧、势力抓手、长期压力源、关系轴线和读者承诺，"
        "让人物与地图、文化、势力网络互相咬合。"
    ),
    "book_blueprint": (
        "当前阶段负责搭建整本书多 Arc 蓝图。重点是每段 arc 都有清晰目标、风险、兑现方向，并且全书章节区间连续、逐级升级。"
    ),
    "bootstrap": (
        "当前阶段负责把 Genesis 根蓝图转成写作执行契约。不要扩写新设定，只把已有 Genesis 成果整理成明确的启动条件与运行规则。"
    ),
}
_GENESIS_STAGE_HARD_RULES = {
    "brief": [
        "title 与项目标题保持一致；one_line 要在一句话里说清主角处境、核心冲突或最大卖点。",
        "audience、core_emotion、core_delight、promise 不能是空泛口号，要让后续阶段可直接引用。",
        "guardrails 优先承接已有内容边界，缺失时也只补和题材、调性、平台表达直接相关的约束。",
    ],
    "world": [
        "world_bible.overview 要说清主舞台、力量/秩序来源和长期冲突方向，不要写百科全书摘要。",
        "axioms 必须体现代价、限制或秩序张力，能支撑长期升级，不能全是万能设定。",
        "culture_profiles 要可复用到命名与人物/地图生成，至少给出清晰语感说明和一组可用示例。",
        "map_atlas 与 story_engine 可以保持骨架，但方向必须和 world_bible 一致，不能出现脱节设定。",
    ],
    "map": [
        "submaps、regions、nodes、edges 必须互相对应，空间层级清晰；regions 最多两级，level=2 必须挂到有效 parent_region_id。",
        "id 要稳定、可复用、易于程序读取，不要只返回名字没有 id。",
        "每个关键舞台都要体现移动成本、控制权、危险或资源主题，避免纯装饰性地点。",
        "如果已有 culture_profiles，优先把文化 id 绑定到 submaps、regions、nodes。",
    ],
    "story_engine": [
        "core_cast 不是人名清单；每个核心角色都要有欲望、恐惧、秘密或长期矛盾来源。",
        "factions 与 opposition 要和地图/地区/据点形成对应关系，不能悬空。",
        "reader_promises 与 long_arcs 要能解释全书为什么值得追更，而不是重复世界观说明。",
        "优先保留并复用已有 culture_profile_id、home_subworld、home_region、base_region 等锚点。",
    ],
    "book_blueprint": [
        "arcs 必须覆盖全书目标章节数，chapter_start/chapter_end 连续无重叠，chapter_count 与区间一致。",
        "每个 arc 都要有新的目标、风险升级和 payoff_direction，不能只是把 premise 重写一遍。",
        "target_size、soft_min、soft_max 要贴近建议尺寸，不要极端失衡。",
    ],
    "bootstrap": [
        "只整理已有 Genesis 结果，不发明新的 operation_mode 或治理字段。",
        "root_ready 必须反映当前 Genesis 根层是否足以启动写作；start_policy 要简洁、可执行。",
    ],
}
_GENESIS_REFINE_SYSTEM_PROMPT = (
    "你是中文长篇网文的 Genesis 协作编辑。你要把用户的口语化修改意图翻译成结构化 JSON 变更，并尽量保持原有蓝图稳定。\n"
    "必须只输出 JSON，不要 markdown、解释或对话腔。\n"
    "优先局部改动：保留未被点名的字段、id、命名体系、隶属关系和上游约束；新增内容必须与已有地图、文化、势力、章节规划兼容。"
)


class StaleGenesisRevisionError(RuntimeError):
    pass



__all__ = [name for name in globals() if not name.startswith("__")]
