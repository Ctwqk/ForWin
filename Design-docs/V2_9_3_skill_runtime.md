# ForWin V2.9.3 Skill Runtime / Workflow Skills 设计规格

更新时间：2026-04-21

来源：`V2_9_2.md` 当前主干架构 + `V2.9.3` Skill Runtime 增量实现。

说明：本文件定义 `SKILL.md` 类工作流在 ForWin 产品运行时中的接入方式。目标不是把外部 Superpower 作为插件挂进生产链路，而是把 Skills 吸收为 ForWin 自己的 prompt / workflow layer。V4.5.1 只收束 prompt-layer governance、project policy 与 PromptTrace 口径；Skill API / UI、脚本型 Skill、tool-backed execution 归入 V4.6+，不作为 V4.5.1 未完成项。

---

## 1. 设计目标

`V2.9.3` 新增一层内部 Skill Runtime，用来承载 Genesis、Writer、Reviewer 阶段的可版本化工作流说明。

核心目标：

- 把 `SKILL.md` 作为 ForWin-native workflow layer
- 让 Skill 参与 prompt assembly，但不绕过根真相与治理边界
- 统一把 Skill 激活记录写入 `PromptTrace`
- 保持 `DecisionEvent` 仍是唯一动作真值
- 为后续多 provider LLM 兼容预留 `ModelAdapter`

本轮边界：

- 只做后端
- 只覆盖 `Genesis + Writer + Reviewer`
- 只支持 `instruction_only`
- 不做脚本型 Skill
- 不做项目级 skill pinning
- 不做 Skill API / UI

---

## 2. Skill Runtime 定义

### 2.1 Skill 资源目录

运行时固定读取仓库根目录下的：

```text
forwin_skills/
```

首批内置目录：

```text
forwin_skills/
  genesis/
    brief/
    world-bible/
    map-atlas/
    story-engine/
    book-blueprint/
    bootstrap/
  writer/
    chapter-outline/
    scene-drafting/
    style-control/
  reviewer/
    chapter-continuity/
    repair-plan/
```

每个 skill 是一个目录，至少包含一个 `SKILL.md`。

### 2.2 Skill Manifest

首批 frontmatter 只支持最小字段：

- `name`
- `version`
- `description`
- `forwin_scope`
- `stage_keys`
- `task_families`
- `mode`

其中：

- `forwin_scope` 只能命中 `genesis / writer / reviewer`
- `mode` 首批固定为 `instruction_only`

### 2.3 运行时模块

新增内部模块：

```text
forwin/skills/
  models.py
  loader.py
  registry.py
  router.py
  prompt_layer.py
  policy.py
```

职责：

- `models.py`：定义 `SkillManifest / SkillSelection / SkillLayer / SkillCapability`
- `loader.py`：从 `forwin_skills/**/SKILL.md` 读取 manifest 和正文
- `registry.py`：管理清单、版本、hash 与过滤
- `router.py`：按 `scope + stage_key + task_family` 选择技能
- `prompt_layer.py`：将技能转成 prompt layers 并插入消息
- `policy.py`：限制 mode / strictness 等基础策略

---

## 3. Skill 与根真相边界

### 3.1 Skill 允许做什么

- 提供阶段性工作流指令
- 提供结构化审稿 rubric
- 提供 repair 建议
- 参与 prompt 组装
- 在 trace 中留下版本化足迹

### 3.2 Skill 不允许做什么

- 不直接写 `Canon`
- 不静默修改 `BookGenesisPack`
- 不绕过 `DecisionEvent`
- 不替代治理 hard gate
- 不直接覆盖 reviewer 最终 `verdict`

因此，Skill 是 prompt / workflow layer，不是第二动作真值。

---

## 4. PromptTrace 与 DecisionEvent

### 4.1 PromptTrace

自动 Skill 激活只写 `PromptTrace`。

`prompt_layers` 新增 `kind="skill"` payload，包含：

- `skill_id`
- `skill_version`
- `skill_hash`
- `path`
- `activation_reason`
- `mode`

`input_snapshot` 新增：

- `selected_skills`

`output_summary` 新增：

- `skill_summary`

### 4.2 DecisionEvent

本轮不为“自动命中 Skill”单独创建 `DecisionEvent`。

只有现有 Genesis / review / rewrite / governance 动作继续沿用既有事件链。后续如果出现显式 enable/disable、manual override、experimental skill 之类的配置动作，再写入 `DecisionEvent`。

---

## 5. LLM 层兼容策略

### 5.1 ModelAdapter

新增内部抽象：

- `ModelAdapter`
- `ModelCapabilities`

Skill Runtime 只读取模型能力声明，不直接绑定具体 provider 协议。

### 5.2 首批实现

首批只实现：

- `OpenAICompatibleAdapter`

现有 `forwin/writer/llm_client.py` 保持原来的 OpenAI-compatible 行为，但对外暴露 adapter 语义。

本轮不做：

- Claude / Gemini 原生 adapter
- tool calling 适配
- script-backed skills

---

## 6. Genesis 接入

Genesis 六阶段全部接入 Skill Router：

- `brief`
- `world`
- `map`
- `story_engine`
- `book_blueprint`
- `bootstrap`

组装顺序：

```text
base stage prompt
-> skill layers
-> genesis context
-> task payload
-> ModelAdapter.chat()
```

`launch_arc_*` 也复用同一套 router，但路由归并到 `book_blueprint` 语义。

---

## 7. Writer 接入

Writer 技能接入点放在 orchestrator，而不是直接散落到 `ChapterWriter` 内部做动态选择。

当前规则：

- orchestrator 先选 `writer` skills
- `ChapterWriter` 只接收已经构建好的 `skill_layers`
- 组合顺序固定为：

```text
base system
-> governance/runtime constraints
-> skill layers
-> context
-> task
```

首批 writer skills：

- `writer.chapter-outline`
- `writer.scene-drafting`
- `writer.style-control`

每次 writer 调用都把 Skill 摘要写入 `generation_meta.prompt_trace`，再由 orchestrator 持久化到 `PromptTrace` 表。

---

## 8. Reviewer 接入

Reviewer skills 只提供 rubric / explanation / repair guidance。

接入规则：

- orchestrator 先选 `reviewer` skills
- `HistoricalReviewHub / WebNovelExperienceReviewer` 只消费这些 rubric
- skill 输出只能进入 review notes、repair guidance、trace payload
- 最终 `pass / warn / fail` 仍由既有 continuity / governance / reviewer 结果决定

首批 reviewer skills：

- `reviewer.chapter-continuity`
- `reviewer.repair-plan`

---

## 9. 全局配置

新增全局默认配置：

- `skill_runtime_enabled`
- `skill_registry_path`
- `skill_strictness`
- `enabled_skill_groups`
- `disabled_skill_ids`

本轮只支持全局默认，不做项目级持久化与 pinning。

---

## 10. 安全策略

P0 / P1 固定只允许 `instruction_only`。

明确不开放：

- 任意 shell
- 任意文件写入
- 任意联网
- 直接写 `Canon`
- 直接改 `Genesis root`
- 直接 override checkpoint verdict

后续如果要支持 tool-backed / script-backed skills，必须经过单独的 capability 和安全设计。

---

## 11. MVP 迁移计划

当前主干已经落地的 MVP：

1. 新增 `forwin/skills/` 运行时基础层
2. 新增 `forwin_skills/` 首批内置技能
3. Genesis 六阶段接入 skill routing
4. Writer / Reviewer 接入已解析 skill layers
5. `PromptTrace` 持久化 skill prompt layers 与 selected skills
6. 新增 `ModelAdapter` 抽象并落一个 OpenAI-compatible 实现

后续阶段按 V4.5.1 重新归档：

- V4.5.1：只收束 prompt-layer skill governance，包括项目级 policy 语义、strictness 启用/禁用口径、PromptTrace 可解释性，以及“不绕过 BookState canon gate”的约束。
- 后续产品化版本：Skill API / UI、脚本型 Skill runtime、tool-backed / read-only tool skills。
- 已不作为本文档残项：多 provider adapter 基础已由现有 LLM compatibility / runtime profile 链路承担，后续只在模型治理设计中继续扩展。
