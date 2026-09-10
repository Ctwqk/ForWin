# ForWin Design Status

更新时间：2026-09-09

状态：active-current。本文档给当前保留的设计文档标注阅读顺序和权威等级。

## 状态枚举

- `active-current`：当前架构入口或当前实现规格。
- `active-maintenance`：仍在维护的专项规则、内容库或操作说明。
- `baseline-with-overrides`：历史主链基线，但必须被当前文档覆盖解释。
- `legacy-compatibility`：只描述兼容、迁移、投影或历史边界。
- `historical-plan`：已执行或被后续设计覆盖的实施计划，不作为目标架构依据。
- `future-product-backlog`：后续产品化方向，不作为当前后端缺口。

## 当前路线图

[2026-09-09 三阶段设计](../docs/superpowers/specs/2026-09-09-forwin-three-stage-design.md) 是已批准的实施契约。其优先级高于旧路线图中的重复 L200/矩阵要求和破坏性迁移约定。实施进度见[执行计划](../docs/superpowers/plans/2026-09-09-forwin-three-stage.md)；未完成项不能解释成当前能力。

历史运行只证明当时的源码、模型和策略。下文历史长跑待办不再是本轮前置条件。

## 当前入口

| 文档 | 状态 | 说明 |
|---|---|---|
| `CURRENT_DESIGN.md` | active-current | 当前实际设计的完整入口，描述主流程、owner、默认策略、阻断条件、已删范围和剩余耦合。 |
| `../docs/superpowers/specs/2026-09-04-v5-autonomy-fixes-design.md` | active-current | 外部评审阶段一：逐合同三态验证、trace outbox补传、同日阻断恢复及一次批次原子预留；不扩展V5主架构。 |
| `CURRENT_ARCHITECTURE.md` | active-current | 精简架构边界，固定 RuntimePolicy v2 / report-only Generation Audit / application boundary / BookState / BookMap / review 口径。 |
| `DESIGN_STATUS.md` | active-current | 本状态清单。 |
| 2026-09-04 收口设计 | historical-plan | 全文见下方 Git 历史入口；重复验收要求由当前三阶段路线图覆盖。 |
| `../forwin_architecture_consolidation_audit.md` | historical-plan | 2026-07-09 架构收敛审计（历史论证记录）。其 Phase A-D 已由 v5 hard-cut 完成并替代，Phase A-F 不再作为待办；source-of-truth 思维与测试/风险框架由后续计划继承。 |
| `../docs/superpowers/specs/2026-07-09-forwin-v5-architecture-convergence-design.md` | historical-plan | 旧 v5 收敛决策记录；破坏性迁移条款已由三阶段设计的备份、隔离验证与向前迁移要求取代。 |
| 2026-07-12 v5 最终路线图 | historical-plan | 全文见下方 Git 历史入口；不是当前待办。 |
| `../docs/superpowers/plans/2026-07-12-forwin-measurement-loop.md` | baseline-with-overrides | S1/S3/S2 已实现并冻结；S4-S8 属发布后改进，不阻塞 v5。 |
| `V4.5_markstone.md` | active-current | 当前代码与设计差距统一入口，旧 `world_model_v4` 已降级。 |
| `V4.5.1_markstone.md` | active-current | V4.5 后端闭环后的残余 contract / 文档 / 测试收束。 |
| `V4_final_book_state_runtime.md` | active-current | BookState 最终 runtime 规格。 |
| `map_scheme_c.md` | active-current | Scheme C BookMap 最终语义。 |
| `writing_flow_state_machine.md` | active-current | 当前写作任务状态机。 |
| `V4.6_knowledge_system.md` | active-current | BookState DB Canon -> Obsidian -> LLM KB 权威关系。 |
| `trope_library_pulp_v1.md` | active-current | Pulp 爽点库当前 runtime seed；运行时扩展到 50+ template，并携带 genre/audience/platform/payoff metadata。 |

## 维护文档

| 文档 | 状态 | 说明 |
|---|---|---|
| `V4.7_character_personality_skill.md` | active-maintenance | Character Personality Skill Library 设计。 |
| `V4.7_character_personality_maintenance.md` | active-maintenance | personality skill 内容、runtime compression、reviewer 和 World Studio 维护。 |
| `V4.8_character_creation_personality_assignment.md` | active-maintenance | 人物创建与自动 personality assignment 设计。 |
| `V4.8_character_creation_personality_maintenance.md` | active-maintenance | 人物创建 helper、assignment、coverage、metrics 和维护流程。 |
| `maintenance_log.md` | active-maintenance | 项目总维护日志。 |

## 历史基线与兼容说明

| 文档 | 状态 | 说明 |
|---|---|---|
| `V2_9_2.md` | baseline-with-overrides | Genesis / Writer / Review / Governance 主链基线；SubWorld / world model 语义以 V4.5、BookState 和 Scheme C 覆盖。 |
| `V2_9_3_skill_runtime.md` | baseline-with-overrides | ForWin-native instruction-only Skill Runtime；API/UI/script/tool-backed 属 future。 |
| `V3_8.md` | baseline-with-overrides | backend observability / audit / PromptTrace 规格；dashboard/SLO 属 future。 |
| `provisional_mechanism_check.md` | legacy-compatibility | Provisional Band Preview 物理删除的历史证据；仅供追溯，不是当前 runtime、策略字段或目标架构。 |
| `review_fix_log_2026-04-15.md` | legacy-compatibility | 历史 review 修复记录。 |

## 保留的决策与历史入口

- BookState DB Canon、唯一 Canon 接纳入口、版本化 RuntimePolicy、持久任务和 outbox 继续有效。
- 已删除的 provisional/Scenario/旧世界状态/反向导入不重新实现；原规则库和回归 fixture 保留。
- 旧 L200/历史矩阵不再阻塞本轮；真实运行记录保留其原始通过、失败或不完整结论。
- 当前生产的旧 migration revision 与主线不同，完整源码升级必须先通过向前迁移及隔离恢复验证。
- 11个旧热修复源码副本已在完整候选镜像与兼容回归通过后从当前树退休；[原目录及装配脚本](https://github.com/Ctwqk/ForWin/tree/521228871a5752ebe8572c057caa9f4944bb0295/deploy/forwin-runtime-hotfixes)保留在基线历史，现有回滚镜像保留。普通构建只使用根目录 Dockerfile 和唯一 `forwin/` 源码树，不恢复运行时字符串拼补。

以下全文已存在于不可变源码提交 `521228871a5752ebe8572c057caa9f4944bb0295`，从当前树退休，未改写 Git 历史：

- [2026-09-04-v5-closure-design.md](https://github.com/Ctwqk/ForWin/blob/521228871a5752ebe8572c057caa9f4944bb0295/docs/superpowers/specs/2026-09-04-v5-closure-design.md)
- [2026-09-04-v5-closure.md](https://github.com/Ctwqk/ForWin/blob/521228871a5752ebe8572c057caa9f4944bb0295/docs/superpowers/plans/2026-09-04-v5-closure.md)
- [2026-07-12-forwin-v5-final-roadmap.md](https://github.com/Ctwqk/ForWin/blob/521228871a5752ebe8572c057caa9f4944bb0295/docs/superpowers/plans/2026-07-12-forwin-v5-final-roadmap.md)
- [DESIGN_STATUS.md](https://github.com/Ctwqk/ForWin/blob/521228871a5752ebe8572c057caa9f4944bb0295/Design-docs/DESIGN_STATUS.md)

需要审计具体历史时可运行 `git show 521228871a5752ebe8572c057caa9f4944bb0295:<path>`。当前日常阅读从 CURRENT_DESIGN 和三阶段路线图开始；不要把历史执行状态当成本轮验收。
