# ForWin Design Status

更新时间：2026-05-06

状态：active-current。本文档给当前保留的设计文档标注阅读顺序和权威等级。

## 状态枚举

- `active-current`：当前架构入口或当前实现规格。
- `active-maintenance`：仍在维护的专项规则、内容库或操作说明。
- `baseline-with-overrides`：历史主链基线，但必须被当前文档覆盖解释。
- `legacy-compatibility`：只描述兼容、迁移、投影或历史边界。
- `historical-plan`：已执行或被后续设计覆盖的实施计划，不作为目标架构依据。
- `future-product-backlog`：后续产品化方向，不作为当前后端缺口。

## 当前入口

| 文档 | 状态 | 说明 |
|---|---|---|
| `CURRENT_ARCHITECTURE.md` | active-current | 当前唯一架构入口，固定 BookState / BookMap / review / compatibility 口径。 |
| `DESIGN_STATUS.md` | active-current | 本状态清单。 |
| `V4.5_markstone.md` | active-current | 当前代码与设计差距统一入口，旧 `world_model_v4` 已降级。 |
| `V4.5.1_markstone.md` | active-current | V4.5 后端闭环后的残余 contract / 文档 / 测试收束。 |
| `V4_final_book_state_runtime.md` | active-current | BookState 最终 runtime 规格。 |
| `map_scheme_c.md` | active-current | Scheme C BookMap 最终语义。 |
| `writing_flow_state_machine.md` | active-current | 当前写作任务状态机。 |
| `V4.6_knowledge_system.md` | active-current | BookState DB Canon -> Obsidian -> LLM KB 权威关系。 |

## 维护文档

| 文档 | 状态 | 说明 |
|---|---|---|
| `V4.7_character_personality_skill.md` | active-maintenance | Character Personality Skill Library 设计。 |
| `V4.7_character_personality_maintenance.md` | active-maintenance | personality skill 内容、runtime compression、reviewer 和 World Studio 维护。 |
| `V4.8_character_creation_personality_assignment.md` | active-maintenance | 人物创建与自动 personality assignment 设计。 |
| `V4.8_character_creation_personality_maintenance.md` | active-maintenance | 人物创建 helper、assignment、coverage、metrics 和维护流程。 |
| `maintenance_log.md` | active-maintenance | 项目总维护日志。 |
| `forwin_design_cleanup_update_plan.md` | active-maintenance | 本轮设计收束与冗余模块更新计划。 |
| `forwin_decoupling_plan.md` | active-maintenance | 架构解耦建议；部分低风险拆分已落地。 |

## 历史基线与兼容说明

| 文档 | 状态 | 说明 |
|---|---|---|
| `V2_9_2.md` | baseline-with-overrides | Genesis / Writer / Review / Governance 主链基线；SubWorld / world model 语义以 V4.5、BookState 和 Scheme C 覆盖。 |
| `V2_9_3_skill_runtime.md` | baseline-with-overrides | ForWin-native instruction-only Skill Runtime；API/UI/script/tool-backed 属 future。 |
| `V3_8.md` | baseline-with-overrides | backend observability / audit / PromptTrace 规格；dashboard/SLO 属 future。 |
| `provisional_mechanism_check.md` | legacy-compatibility | legacy provisional 边界说明；当前判断路径是 Scenario Rehearsal、Candidate Draft Review 和 BookState gate。 |
| `review_fix_log_2026-04-15.md` | legacy-compatibility | 历史 review 修复记录。 |

## 历史实施计划

| 文档 | 状态 | 说明 |
|---|---|---|
| `docs/superpowers/plans/2026-04-24-forwin-v4-world-model.md` | historical-plan | 解释 `world_model_v4` / `reviewer_v4` side-by-side 来源；已被 BookState final 覆盖。 |
| `docs/superpowers/plans/2026-04-24-forwin-v4-1-runtime-hardening.md` | historical-plan | V4.1 hardening 计划；“V4 source semantics” 口径已被 BookState final 覆盖。 |

## Future Product Backlog

以下内容不作为当前 V4.5.x 后端缺口：完整 World Studio 图谱/地图/认知 UI、metrics dashboard / SLO 看板、Skill API / UI / script-backed / tool-backed execution、native GraphDelta extractor、完整 world/map/cognition rule pack、复杂交通工具体系、可视化地图编辑器、Neo4j 主存储、tile renderer、Genesis 深层 workflow editor、自动 retcon accepted canon、完整 LLM editorial reviewer 产品化。
