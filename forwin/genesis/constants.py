from __future__ import annotations

import logging
import re



logger = logging.getLogger(__name__)

GENESIS_STAGE_ORDER = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "world_seed",
)
_STAGE_TO_SECTION = {
    "brief": "book_brief",
    "world": "world",
    "map": "world.map_atlas",
    "story_engine": "world.story_engine",
    "book_blueprint": "book_arc_blueprint",
    "world_seed": "world_seed",
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
    "world_seed": "初始世界",
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
    "world_seed": (
        "当前阶段负责把已锁定的 Genesis 蓝图展开成开篇时刻的初始世界：本书自己的日历与词表，每个角色与势力的位置、资源、议程和初始意图，"
        "关系边，事实与秘密的知情分布，规则、时限、读者承诺与揭示候选。只引用上游已有的实体与地点 id，不新增角色、势力或地点。"
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
        "scope、danger_level 与 edges 必须遵守输出要求中的枚举、整数范围、端点引用和布尔类型约束；未知危险等级用 null。",
    ],
    "story_engine": [
        "core_cast 不是人名清单；每个核心角色都要有欲望、恐惧、secrets 字符串列表或长期矛盾来源，不输出 secret。",
        "core_cast、factions、opposition 的 id 必须稳定且全局唯一，分别使用 cast-{n}、faction-{n}、opp-{n} 默认值；factions 与 opposition 要和地图/地区/据点形成对应关系，不能悬空。",
        "relationship_axes 必须是含 source、target、edge_type、note、visibility 的对象列表；source/target 引用实体 id，edge_type 使用允许的世界关系类型，visibility 只能为 public 或 secret，默认 public。",
        "reader_promises 保持字符串列表；long_arcs 使用含 id、summary、owner_refs 的对象列表，默认 id 为 long-arc-{n}，owner_refs 引用实体 id；两者要能解释全书为什么值得追更，而不是重复世界观说明。",
        "优先保留并复用已有 culture_profile_id、home_subworld、home_region、base_region 等锚点。",
    ],
    "book_blueprint": [
        "arcs 必须覆盖全书目标章节数，chapter_start/chapter_end 连续无重叠，chapter_count 与区间一致。",
        "每个 arc 都要有新的目标、风险升级和 payoff_direction，不能只是把 premise 重写一遍。",
        "target_size、soft_min、soft_max 要贴近建议尺寸，不要极端失衡。",
    ],
    "world_seed": [
        "checkable 只能使用输出要求所列的五种 kind；不能用这些 kind 表达的规则 checkable 填 null。",
        "actors 必须为 story_engine 中每个 core_cast、opposition、factions 条目各给出一项，ref 等于该实体 id；不得新增、遗漏或重复实体，所有引用只使用上游已有的 id。",
        "major 行动者必须有 1–3 个 agenda；角色为 major 时必须给出 location_ref，为 minor 时也应给出；location_ref 只能取地图节点表中的 id。",
        "所有时长（intents.scheduled_in、clocks.due_in）都写成 {amount, unit}：amount 为非负数，unit 只能是本书 calendar.units 或 lexicon.time_unit_aliases 中的名称；单位名称按本书题材自拟。",
        "facts 每项都必须显式给出 sensitivity；hidden 或 secret 的事实必须在 knowledge 中至少有一个 state 为 knows、believes 或 suspects 的知情者，否则标记 nobody_knows: true。",
        "relations 每项都必须显式给出 visibility，source 与 target 只引用行动者 ref；优先承接 story_engine.relationship_axes，不凭空制造关系。",
        "knowledge 的 channel 一律为 seeded；可以包含误信或怀疑，但 fact_id 必须引用 facts 中的 id。",
        "core_cast 的 secrets 要展开为 facts 并配置知情分布；reveal_candidates 的 arc_number 只能取卷列表中的编号，只作揭示参考，不做硬绑定。",
        "clocks 只记录开篇时已经存在的时限；rules 区分 hard 与 soft，只有能被程序判定的规则才填写 checkable。",
    ],
}
_GENESIS_REFINE_SYSTEM_PROMPT = (
    "你是中文长篇网文的 Genesis 协作编辑。你要把用户的口语化修改意图翻译成结构化 JSON 变更，并尽量保持原有蓝图稳定。\n"
    "必须只输出 JSON，不要 markdown、解释或对话腔。\n"
    "优先局部改动：保留未被点名的字段、id、命名体系、隶属关系和上游约束；新增内容必须与已有地图、文化、势力、章节规划兼容。"
)


class StaleGenesisRevisionError(RuntimeError):
    pass



__all__ = ["GENESIS_STAGE_ORDER", "StaleGenesisRevisionError"]
