# ForWin Design Status

更新时间：2026-10-22

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
| `CURRENT_DESIGN.md` | active-current | 当前实际设计的完整入口，描述主流程、owner、默认策略、阻断条件、已删范围和剩余耦合。 |
| `../docs/superpowers/specs/2026-09-04-v5-autonomy-fixes-design.md` | active-current | 外部评审阶段一：逐合同三态验证、trace outbox补传、同日阻断恢复及一次批次原子预留；不扩展V5主架构。 |
| `CURRENT_ARCHITECTURE.md` | active-current | 精简架构边界，固定 RuntimePolicy v2 / report-only Generation Audit / application boundary / BookState / BookMap / review 口径。 |
| `DESIGN_STATUS.md` | active-current | 本状态清单。 |
| `../docs/designs/2026-10-20-world-engine-overhaul.md` | active-current | 世界引擎大修设计（破坏性）。P0–P3（护栏、world_seed / BookState v6、统一章节 delta、世界 tick 与冲突闭环）已落地；P4 为后续设计，推迟事项见下文。 |
| `../docs/superpowers/specs/2026-09-04-v5-closure-design.md` | active-current | 最新收口复评：以 fdaeaa6 领先候选为基线，冻结已完成 Track A/B0，修正重复验收与 tag-only 附加门；保留真实恢复及最终全新 L200 no-hotfix。 |
| `../forwin_architecture_consolidation_audit.md` | historical-plan | 2026-07-09 架构收敛审计（历史论证记录）。其 Phase A-D 已由 v5 hard-cut 完成并替代，Phase A-F 不再作为待办；source-of-truth 思维与测试/风险框架由后续计划继承。 |
| `../docs/superpowers/specs/2026-07-09-forwin-v5-architecture-convergence-design.md` | active-current | v5 破坏性收敛规格；RuntimePolicy v2，旧项目和旧设置不迁移。 |
| `../docs/superpowers/plans/2026-07-12-forwin-v5-final-roadmap.md` | historical-plan | 执行顺序已被《复核执行版最终总方案》及 2026-09-04 收口修订覆盖；不能把旧 checkbox 当当前待办。 |
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

## 2026-10 世界引擎大修 P3（2026-10-22）

状态：P3 代码收口。下列 P2 抽取 / 审查能力继续保留；当前迁移头为 `0005_world_plan_conflicts`（父为 `0004_drop_parallel_ledgers`）。

- **单次抽取与翻译**：每个候选正文由 `ChapterDeltaExtractor` 在 `chapter_delta_extraction` stage 完成一次统一抽取（校验失败可重试）；`ensure_chapter_delta` 按 `body_sha256` 复用结果，正文改变才重抽。输入包括 BookState as-of N−1 的最小实体表、地点表、计划目标 / 义务 / reward targets、intents、secrets、calendar units、time aliases 和 travel modes，不是 ChapterWorldView。`ChapterDeltaTranslator` 将 `WriterOutput.chapter_delta` 翻译为 `source_type="chapter_extraction"` 的 GraphDelta，再经 `BookStateReviewGate` v6；draft review 和 canon preparation 共用这份抽取，最终仍由 `CanonAdmissionService.commit_plan` 原子提交。
- **世界审查**：`WorldConsistencyReviewer` 的 `world_consistency` draft-review 信号运行相同 translator 与 gate，把错误送进 repair；它替代已删除的 `MapMovementReviewer` / `forwin/review/map_movement.py`。v6 检查时间跨度、移动偏移、已知路径可达性、`review.travel_tolerance` 下的速度、受限角色移动、章前已死亡实体行动、物品持有者、告知来源和读者揭示时间表。地图边 `travel_time` 为小时，乘 60 换成整数分钟；本书时间单位统一经 `duration_to_minutes` 换算。
- **结构合同**：移动存于 delta `metadata["movements"]`；物品只认 item 的 `state.holder_id`。条目级 `flashback: bool` 加 `time.evidence` 表示闪回；闪回只产生事实与知识事件，不改变当前 state。非 create 结构补丁带 `old_value` 与 `field_path`。计划 goals 与 narrative obligations 由有正文证据的结构化 `plan_fulfillment` 判定，已删除关键词子串检查。
- **删除与 schema**：`WriterContractDeltaBuilder`、`BookStateGraphDeltaExtractor`、`pulp_light` 正则兜底、旧分阶段 extraction、倒计时/羁押 future-plan audits 和 canon_quality 的 countdown / character-state ledgers 已删除。P2 的 `0004_drop_parallel_ledgers`（父为 `0003_book_state_v6`）删除 `countdown_ledgers` / `character_state_transitions`。chapter review form 继续作为 LLM 信号，其行来自 BookState，回答只产出 quality signals，不再写任何平行账本。
- **Genesis 与原子开写**：Genesis map 提供结构化 submaps / regions / nodes / edges；`forwin.map.genesis_adapter` 将其转换为 BookMap。handoff 依次物化 arcs → map → chapter-0 world_seed → arc-1 ChapterPlan，service / seed commit 只 flush，由调用方提交；`HandoffStageError` 回滚交接写入后只保留失败事件。成功时才锁定 Genesis revision 并设为 writing。
- **护栏收口**：`tests/overfit_allowlist.toml` 的 `meta.completed_phase = "P3"`，`pending_removal` 只能指向未来阶段；canary 的时间单位 ignore 及过时注释已删除。

- **当前揭示约定**：world_seed 的 `location_ref` 在提交时换成数据库地图节点 id；`reveal_candidates` 保留在冻结 Genesis pack，P2 的 `reveal_schedule.earliest_reveal_chapters` 取对应卷的 `chapter_start`；无时间表时向读者推进记 warning。`reveal_directives` 留到 P4。

### P3 已落地

- **tick**：post-Canon 固定 `("world", "planning", "arc", "feedback")`，world 先 `WorldTickService.run` 后旧 `WorldSimulator`。tick 以 `tick-<commit id>`、本章整数分钟时间窗口与 seed 运行；disabled / 非最新章 / 已 tick 章跳过，`target_total_chapters > 0` 且本章达到或超过目标时以 `skipped_reason="book_complete"` 跳过。intent interactions、clock producers、due intents、onstage bookkeeping（P3 临时顺延规则，P4 band 规划器替换）与 `ActorTurnRunner` 产候选，过 hard rules / 逐候选 gate、一次重试（actor feedback 脱敏）、impact / budgets / narrative boundaries、冲突审查、一轮新闻传播，再由 `WorldTickDeltaBuilder` 构建。LLM 故障跳过节点，不使 tick 失败。
- **准入与重写**：`CanonAdmissionService.commit_world_tick` 以 tick_id 幂等、在最新章以 `FOR NO KEY UPDATE` 项目锁写入。提交前 `RunningStateCheck` 先逐个检查 base candidates，再按组检查 in-tick resolutions：hard rules 读取累计接受后的 runtime，world gate 检查累计 delta；base 失效以 `constraint_violation` 丢弃，resolution 失效以 `failure_reason="conflict_stale"` 升级人工、保持 open。无补丁且无 resolution 的 quiet tick 不取项目锁；其余在 commit SAVEPOINT 前取锁并复核 snapshot（P27）。world step `result_json` 保留 `tick_id`、`graph_delta_ids`、`conflict_ids`、`dropped_candidate_ids`、`postponed_candidate_ids`、`warnings`、`skipped_reason` 及模拟器 `pressure_level`。历史重写 CAS 重放 world_engine deltas，失败者退役并产生 `replay_cas` 人工冲突；依赖其创建引用的 tick 级联丢弃、非 tick 依赖拒绝整次重写。
- **冲突与 Codex**：`world_plan_conflicts` 保存待决变化和审查 / 阻塞信息。historical_rewrite / critical → human，minor → auto veto_world，moderate / major → Codex（`world.conflict_delegate="codex"`）或 human。`WorldConflictDelegate` 按 `models.role_preferences["review.world_conflict"]` 默认用 `gpt-6-astra`，目录由 `FORWIN_CODEX_MODELS` 声明；与 Spark 共用 `run_codex_structured_call` 和独立事务审计。失败或 `amend_plan` 升级人工。
- **选项**：historical_rewrite 仅 veto；protection / milestone / reveal_schedule 仅 veto / amend；其他提供 veto / plot_responds / amend，minor / moderate 另有 accept_with_obligation。veto 标未结 intent 为 vetoed，并在 `[latest - world.max_rerolls, latest]`（两端均含）检测到的冲突中，统计该 actor 的 `veto_world` 决定次数；次数小于 `world.max_rerolls` 时让 actor 再行动；accept 经 `DeferAcceptanceTransaction` 提交 `world_change_response` 义务，deadline 为 `min(latest+6, target)`（`latest` 为最新 canon 章，`target` 为 `project.target_total_chapters`）；plot_responds 为下一个未接受计划加目标；人工 amend 提交变化并解除破坏的 protection。直接 resolve 用 `commit_world_tick(tick_id="conflict-<id>")`，世界变化、计划 / 义务和关闭冲突同一个 SAVEPOINT。
- **阻塞与入口**：open human conflict 在 `blocks_from_chapter` 起暂停 run，stage 为 `paused_for_review`、任务为 `needs_review`，派生 reason 为 `world_conflict_pending`；continue / review-approve（`continue_generation=true`）返回 409 直到解决。解决后重新读状态、查 active task 再显式继续。HTTP `/api/projects/{id}/world-conflicts[/{cid}[/resolve]]`、MCP `world_conflict_list` / `world_conflict_get` / `world_conflict_resolve` 与 task drawer 已接入；旧信号用 `world_model_signal_list`，保留 `/world-model/conflicts`。
- **裁定与 actor 上限**：裁定只给本次 due intents、其 owner / targets、它们持有的 items / resources 及同地 active actors / items / resources / site_states 完整正文；event 与非本次 due intent 不给完整正文，compact index 排除 event / 已结 intent，保留 pending intents。facts 保留全部 scoped standing facts，事件事实按 `created_at_chapter` 降序、id 升序选最新 `MAX_ADJUDICATION_EVENT_FACTS = 20` 条；允许值含 `ended_status`。每 tick 先尝试最多 20 个 due offstage intents 的一批，超 prompt guard 后逐个调用；单个仍超限则 DROPPED `prompt_too_large`，后续 intent 补位，超 cap 未询问者以 `adjudication:<intent>` 进入 `postponed_candidate_ids`。actor 视图按 priority 降序、id 升序只保留 `MAX_ACTOR_AGENDAS = 8` 条 active agendas；超过 `MAX_AGENDA_CHANGES_PER_TURN = 3` 项 agenda_changes 以 `too_many_agenda_changes` 重试，prompt 含 `max_agenda_changes_per_actor`；SQL 为每个实际回合 actor 只读最新五条 veto_world resolutions。
- **影响与 protection**：major 包括 faction / organization / group / site_state 的 `state.status` 从未结束进入 destroyed / dissolved / disbanded / inactive，faction / group 的 `leader_id` 或 site_state 的 `controller_faction_id` 变化，以及 knows / believes 将 hidden / secret / must_not_reveal 事实给予此前未持有该认知的 observer；新隐藏事实的 known_by 不算再次暴露。`must_exist` 同样以进入 ended status 判定。解除 protection 对缺省 `until_chapter` 记 `old_value = 0`（open），compiler CAS 对缺省 `review_constraint.payload.until_chapter` 也读作 0。
- **resolve 与写作文本**：直接 resolve 在冲突行锁后立即取得项目锁，锁内读 canon / news queue，并用同一 running-state 检查复核变化；hard-rule / gate error 返回 `ConflictStale`（HTTP/MCP 409），Canon 拒绝写入也返回 409，冲突保持 open；veto 不受 held change 失效影响。context 与 delta 的 `story_time_start` 使用最新章 extraction 起点至当前故事分钟的窗口。plot_responds / accept_with_obligation 的目标、progress marker、义务 summary / payoff_test、plan-patch contract 及 writer / reviewer injections 只取 held change 的 public event summaries；无公开事件用 `PLOT_RESPONSE_GENERIC_GOAL`，onstage_slip 用 `ONSTAGE_SLIP_RESPONSE_GOAL`，冲突摘要不进入这些写作文本。
- **重写的 resolution 副作用**：被丢弃 world_engine delta（tick 或直接 conflict resolve）承载的 resolutions 被 supersede，原冲突仍为 resolved；未结义务经 `NarrativeObligationRepository.cancel_obligation` 进入终态 `cancelled`，记录 `metadata.cancel_reason`，不再计入 open 列表、预算或 gate。`planned` / `failed` 计划补丁经 `FuturePlanPatchMixin.revert_plan_patch` 回撤并追加 `plan_patch_revert`，accepted 等其他状态计划不变；保留同计划其他仍有效 resolution 补丁共同贡献的目标 / 标记（P34），五项上限曾挤出的旧条目无法恢复。`replay_cas.proposed_change` 总含 `superseded_conflict_ids`、`cancelled_obligation_ids`、`reverted_plan_patch_ids`，有 supersede 时摘要给出三项数量。
- **配套收口**：确定性地图物化用 `sha256(f"{project_id}|{subworld_id}")`，按 Genesis 声明端点、方向、hidden 属性物化边，已有地图不自动重建。到期 clock 生产者已接入；form 仅保护时间为正的最近六个 running clocks，secret clock 描述与后续 writer 约束不暴露 label / node id。`estimate_travel` 共用本书 travel modes / 已知路线的时间计算。

### 与设计的取舍 / 明确推迟到 P4

- **protection 生产者与 critical / 里程碑**：P3 读取 narrative `review_constraint` 的 `{ref, kind, until_chapter, source, observer_ref?, fact_ref?, field?, value?}`，遵守已有 protection；节拍表 / band 规划器尚不生产它们，正常项目通常为空。影响分级接收 `milestone_refs`，当前 tick 使用空集合，尚无里程碑驱动的 critical 生产者。`amend_plan` 已可由用户选择。
- **冲突来源与改章**：P3 生产 `world_tick` / `historical_rewrite` 冲突；`chapter_extraction` / `plan_feasibility` / `band_planning` 冲突生产者及 `rewrite_chapter` 推迟到 P4，P3 从不提供该选项。tick 不写 reader 认知，因此不触发 reveal_schedule 冲突；读者揭示仍由正文抽取推进。
- **世界变量与规则谓词**：`world.drift` 无 institution / resource_economy 世界变量节点；rule 的 `checkable` 尚无 Genesis 谓词 kind，当前不求值，也不因此报错。
- **重写后的补 tick**：最新章重写后若已有 tick，跳过第二次 tick，避免世界重复推进；代价是重写正文自己的事件没有本次 tick 的新闻传播。delta-only re-tick 及对保留 tick 的 open conflicts 重新推理一并推迟到 P4。
- **历史引用与冲突卡片**：M7（P10 扩展）留待 P4：保留 tick 对被替换章节所创建 refs 的引用尚无 CAS 检查。M10 留待 P4：控制台冲突卡片尚不展示 held change，需经 `world_conflict_get` 查看 `proposed_change`。
- **性能与消息重排**：`WorldTickDeltaBuilder` 的 `runtime=` 复用尚未实现，仍有 BookState 重载成本；`_unprotected` 当前丢弃 protection 禁止的投递，改为重新排队留到 P4。
- **writer 世界视图与规划**：writer prompt 不携带 clock / restraint state；替代物是设计 §5.1 的 `ChapterWorldView`。writer 的 `<<FORWIN_LOCATION>>` 标记与 `reveal_directives` 留到 P4 调整；`task_contract_json`、ArcDirector / band 规划、`launch_arc_N`、`finalization._seed_state`、`RunControlStage.run`、`SubWorldManager.apply_initial_arc_plan`、`sub_world_roster_items` / `plan_band_activation` 留待 band `onstage_refs` 与新规划器替换。最终 post-Canon `("world", "plan", "feedback")` 顺序也在 P4 随规划器重写落地。
- **其余未闭环能力**：`director_setup_without_delivery` / `form_invariant_drift` 仍无 producer；`ledger_state_drift_pre_write` patches 持久化但未应用；独立的地图 access rules、team speed、edge multipliers 检查尚未接入；认知图的 `false_edges` 已参与路径查询。意图引出新行动者也留到 P4，P3 tick 只引用已有行动者。

P3 生产部署 runbook 见 [docs/superpowers/plans/2026-10-22-world-engine-p3.md Task 20](../docs/superpowers/plans/2026-10-22-world-engine-p3.md)；本文只记录代码现状与后续设计，不宣称生产验收完成。

## 2026-10 世界引擎大修 P1（历史记录）

P1 代码收口时的记录；当前 P3 状态以上节为准。

- Genesis 顺序为 `brief → world → map → story_engine → book_blueprint → world_seed`，旧 `bootstrap` / `execution_bootstrap` 已删除。`world_seed` 由 LLM 生成，经 `forwin/genesis/world_seed.py` 确定性校验；有 error 时拒绝锁定，fallback 为 degraded 结构壳。story_engine 实体有 id（缺省 `cast-n` / `opp-n` / `faction-n`）、结构化 `relationship_axes` / `long_arcs` 与 core_cast `secrets` 列表；种子为每个 core_cast / opposition / faction 提供 actor，fallback 只按 id 建壳。
- 开写顺序为 arcs → map bootstrap → `WorldSeedCommitter` → arc-1 ChapterPlan；`arc_plan_versions` 保存 `title` / `goal` / `stakes` / `payoff_direction`。种子经 `BookStateReviewGate` 与 `CanonAdmissionService.commit_world_seed` 成为第 0 章、`source_type="genesis_seed"` 的 GraphDelta；提交入口使用项目 `FOR NO KEY UPDATE` 锁。地图 / 种子失败由 `HandoffStageError` 的 `GenesisMapBootstrapError` / `WorldSeedCommitError` 表达，记录 `MAP_GENERATION_FAILED` / `WORLD_SEED_COMMIT_FAILED`。第 0 章 subworld 角色生成路径已删除。
- Alembic 在 P1 的 head 为 `0003_book_state_v6`（父为 `0002_drop_dead_tables`）。故事时间为自纪元起的整数分钟，使用 `graph_deltas.story_time_start/end`、`story_calendars` 和 `format_story_time`；`book_lexicons` 保存单位别名、等级阶梯、货币、称谓、人物泛称、交通模式及消息速度。
- `world_node_states` 按章版本化 name / aliases / profile / is_active，`world_nodes` 是最新投影；narrative 层进入 world snapshot。已有章节的追加编译以该章为基底；世界编辑钳位至最新 canon 章并在项目锁内复核。`canon.book_state_layers` 已删除，四层始终启用。
- 新节点为 `agenda` / `intent` / `clock`，有 v6 值校验；character 规范字段包括 `alive_status` / `restraint` / `travel_mode_ids`，位置保留 `state.location_id`；actor 使用 `actor_tier` / `next_turn_at` / `resources`；rule 使用 `hardness` / `statement` / `checkable`；社交边支持 `rival_of`。知识事件经 `knowledge_by_ref` 写认知，默认可见性按事实敏感度决定。读者承诺统一为 narrative `promise` 节点；`book_reader_promises` 表和 `metadata.reader_promise_patches` 已删除。
- 人物泛称来自本书 lexicon；canon name anchors 来自 BookState kinship 边 `metadata.appellation`，方向为“source 的 appellation → target”。地图从 Genesis 结构推导区域、地形与路线，不再嗅探题材关键词；P1 的地图角色词表、题材人物 token 和关系 personality 映射已清理。
- 与设计的取舍：world_seed 的 `location_ref` 是 Genesis 地图 id，由提交器换成 `state.location_id` 的数据库节点 id；`reveal_candidates` 保存在冻结 Genesis pack，P2 已用于 reader reveal schedule。
- P1 当时延后的章节时间、世界审查和旧账本清理已在 P2 完成，见上节。
- P4：`finalization._seed_state`、`RunControlStage.run`、`SubWorldManager.apply_initial_arc_plan` 与 ArcDirector 一起删除；`sub_world_roster_items` / `plan_band_activation` 留待 band `onstage_refs` 替换；章节规划仍调用 `launch_arc_N`。当时延后的世界引擎已在 P3 落地；节拍表留到 P4。

## 2026-10 世界引擎大修 P0（历史记录）

设计见 `../docs/designs/2026-10-20-world-engine-overhaul.md`。P0 只做护栏与纯删除；当时 `meta.completed_phase` 推进到 `P0`，当前已推进至 P3（见上节）。`ALLOWED_PRODUCTION_MECHANISM_FILES` 已清空。

- 新增防过拟合 AST 护栏：`tests/overfit_guard.py`、`tests/overfit_allowlist.toml`、`tests/test_no_story_content_in_runtime.py`，带按阶段门控的 `pending_removal` 区段。
- 新增 `scripts/purge_all_projects.py`（默认 dry-run，`--yes-delete-all-projects` 才执行；dry-run 回滚式试删以暴露外键阻塞，任一项目删除失败时不清理外部存储并以退出码 4 结束）；`_delete_project` 现在删除 publisher 子表行与项目 outbox 事件。
- 删除 v4 抽取链路：`book_state/extraction` 只剩 `contract.py` 与 `graph_delta.py`，`WriterContractDeltaBuilder` 与 `pulp_light` 正则兜底保留（后者 P2 删除）；`WriterOutput` 的 v4 字段与 `RepairScope` 的 `world_model` 已移除。
- Alembic `0002_drop_dead_tables` 删除 `world_projection_deltas`、`story_obligations`、`reveal_registry_entries`、`artifact_collection_ledgers`、`book_reader_experience_deltas`，并 `DROP TABLE IF EXISTS` 生产库独有、外键阻塞项目删除的 15 张 v4/scenario 遗留表（不用 CASCADE）；当时保留的 `book_reader_promises` 已由 P1 的 `0003_book_state_v6` 删除。
- world contracts 只产出通用合同（homeworld 样例已删）。
- 续写改为追加一个 planned arc，由 arc activation 规划其章节；materializer 为 blueprint 中缺失的卷合成 payload；读模型把此类卷报告为可恢复。
- Genesis fallback 只返回结构壳，`stage_states[stage].degraded` 标记 fallback 输出。
- 删除 writer prompt 的故事示例、ArcDirector 默认世界、SubWorldManager 名字池与 WorldSimulator 的 `chapter>=3` 规则；未命名 planned 槽位不再被激活。
- 清除 canon_quality、checker（pulp profile 只按显式 genre 选择）、canon_names（锚点不分角色，P1 已补入 BookState 来源）与 future-plan audit 中泄漏的第二套剧本词汇。
- 创建项目必须提供 genre；llm_eval fixture 移至 `tests/fixtures/llm_eval`，CLI 必须传 `--context-file` / `--cases-file`。
- 当时延后至 P1 的地图角色词表、题材人物 token、锚点来源与账本 v6 已完成；P4 剩余事项见上节及 allowlist 的 `pending_removal`。以下各节中的 2026-07/09 记录是当时历史，不代表 P3 之后的现状。

## 2026-09 外部评审后的修复

基于已推送的master@0a06cfa，阶段一继续保留V5主体设计，已完成三个已复现问题及交叉复核边界的修复。最终530824e默认2218通过/1跳过、独立RC1156通过，Ruff/compileall通过；没有新增真实运行验收。当前实现和验证边界见[本轮记录](../docs/operations/v5-autonomy-fixes-2026-09-04.md)。下节6161e4c及R27是前轮历史证据，不能替代新代码的回归或实际运行验收。

## 2026-09 V5 前轮收口状态

本轮起点是 `codex/v5-r9-integration-candidate@fdaeaa6`，初次核对时领先 master 147 个提交；修订派生于 `codex/v5-closure`，集成目标为远端默认分支 master。已确认所有其他 v5 worktree 的实现均包含在该候选或已移植。代码集成与正式运行发布分开记录。

- Track A/B0 和 A2/A4 的实现决定均已落地：provisional/Scenario/reverse import 已删，Generation Audit report-only，BookState ownership 已归位；停止重复实现与扩建度量。
- 最终本地验证：6161e4c默认suite 2162通过/1跳过，未再修改的RC harness 1147通过，Ruff/compileall通过。fdaeaa6旧基线的测试环境污染失败记录保留；不把旧SHA或本地PASS改称真实发布PASS。详见[收口验证记录](../docs/operations/v5-closure-reassessment-2026-09-04.md)。
- R27 通过隔离 MCP 实时核实为 85/250 accepted：L30 8/30、L60S 8/60、L60P 60/60、L100 9/100；三个 standard 项目 needs_review，active generation count=0。矩阵为 partial，不是通过。
- L100 暴露的旧 scene text 并列送审及受保护标题漂移已分别修复；真实内容问题未被静默批准。
- V1 和 generation pre/post-commit 恢复存在旧候选真实 PASS；其余当前候选故障证据不齐。`setup_blocked` 不能算恢复 PASS。完整差异见 `.artifacts/rc-candidate/release-evidence-ledger.md`。
- R9 正式 RC 冻结、最终全新 L200 no-hotfix、R11 部署均未完成。原始历史记录保留下文，不能覆盖本节状态。

## 2026-07 V5 Final Roadmap 历史执行记录

状态：R0-R4 implementation complete；旧长跑已安全退出，current-HEAD 测试债已清零，Track A 与发布前 B0 仪表已落地，下一阶段为 R5 发布验证。

- 旧项目 `a06cf00db3ba4cbe8b9862e20e9d6248` 的生成任务已于 2026-07-12 安全暂停，权威 active task count 为 0。
- 该运行固定归类为 `pre-roadmap-soak`，`release_evidence=false`；路线图变更从快照之后开始，因此它不得作为最终 V6 发布证明。
- 不恢复、不清洗该项目；保留为只读回归样本。
- 完整身份、事件、Canon、trace、投影和发布证据见 `../docs/operations/v5-pre-roadmap-soak-snapshot.md`。
- R1 首次可信全量基线为 `97 failed, 1540 passed, 1 skipped`；97 项均已按当前 v5 公共契约修复或删除，不以放宽生产约束换取通过。
- 测试 PostgreSQL harness 改为每个测试及时回收 transient database，browser session database 仅在进程退出时回收；全量运行后遗留测试库为 0。
- R1 最终证据：`1602 tests collected`；全量 `1601 passed, 1 skipped, 112 warnings, 33 subtests passed in 175.31s`；`ruff check forwin tests`、`compileall -q forwin` 和 R1 聚焦门均通过。
- R2 删除 policy 开关、第二 writer、preview service、execution/fallback/repair callback、两张表、事件、HTTP/read-model/task/UI/probe 全链，生产禁止符号零命中；保留 Arc sizing window、promotion/projection 与普通 writer preview fallback。
- R2 最终证据：`1601 tests collected`；全量 `1599 passed, 1 skipped, 110 warnings, 33 subtests passed in 169.70s`，新增 band repair 无 preview callback 回归通过；Ruff、compileall、rendered page 测试、源码扫描和 `git diff --check` 通过。
- R3 完成 BookState extraction、future-plan pre-audit 与 application read model 的 owner 归位；旧 package、wrapper 和 re-export 壳均已删除。
- R4 完成 S1 GateOutcome/GateLedger、S3 成本/人工动作账本与 S2 规则出身制度；对应 MCP 为 `gate_ledger_report`、`cost_report`、`rule_provenance_report`。
- R4 最终证据：全量 `1656 passed, 1 skipped, 110 warnings`；S2 聚焦回归 `179 passed`；`ruff check forwin tests`、`compileall -q forwin tests` 与 `git diff --check` 均通过。
- 最终 200 章 gate 必须等待 A2/A4 数据决策落地与 R9 Release Candidate 冻结后，使用全新项目从第 1 章开始。

## 规则出身与事故响应治理

- 事故响应顺序固定为：修复成因（prompt、抽取或准入契约）→ 冻结 fixture 回归测试 → 仅在仍有必要时创建项目运行规则；禁止用新增全局词条代替成因修复。
- 全局确定性 reference classifier 只保留无歧义语言泛称。技术 ID、职业/组织形态与状态标签只能作为 `genre_candidate` 特征；故事专有名称必须进入项目内 Entity admission，不得静默降级或跨书短路。
- Runtime 生成的 `ActiveRule` 永远 project-scoped，必须从 `observing` 起步；只有显式进入 `active` 的规则可被 BookState invariant 查询消费。规则审计行不进入通用 open-signal 路径。
- 生命周期只能显式执行 `observing -> active -> suspended -> retired`；`suspended -> active` 允许在新证据下恢复。S1 证据只能产生建议，不能自动改状态。
- 跨书证据只产出 `global_promotion_recommended`。真正 global promotion 必须同时具备至少两本书的真阳性、每本书 frozen fixture、静态代码 registry 修改、owner review 与全量测试；不存在 global runtime rule database。
- `start-writing` 的 `START_WRITING_REQUESTED` 事件保存规则体检快照：全局代码规则、本项目规则状态，以及其他项目规则在目标项目中的 `observing_for_target_project` 隔离状态。

## 兼容 / 弃用矩阵

本矩阵是代码迁移的当前权威入口。新增调用方不得再引入 `deprecated` 模块；保留调用方必须通过对应的 current/compat 模块收束。

| 模块 | 状态 | 当前替代 | 删除 / 复核目标 | 说明 |
|---|---|---|---|---|
| `forwin.world_model` | removed | `forwin.knowledge_system` + `forwin.book_state` | 已删除 | live page/proposal/Obsidian/retrieval helper 已迁入 owner；`/world-model/*` 只保留 HTTP 传输契约名。 |
| `forwin.world_model_v4` | removed | `forwin.book_state` | 已删除 | 旧 compatibility projection/debug bridge 已从生产模块删除。 |
| `forwin.world_v4_compat` | removed | `forwin.book_state` | 已删除 | 旧 compatibility projection writer 已从生产模块删除。 |
| `forwin.reviewer_v4` | removed | `forwin.book_state.extraction` | 已删除 | 旧导入 alias 包已物理删除。 |
| `forwin.world_v4_review_gate` + `forwin.extractor` | removed | `forwin.book_state.extraction` | 已删除 | extraction contract 与 GraphDelta converter 归同一 owner；`V4Review*` / `WorldDeltaExtractor` 旧类型无 alias。 |
| `forwin.book_state.extraction` | active-internal | 无 | 无 | 当前 BookState candidate extraction owner，不是主 chapter reviewer。P2 统一使用 `ChapterDeltaExtractor` / `ChapterDeltaTranslator`；`contract.py` / `graph_delta.py` 与 `pulp_light` 已删除；v4 抽取/gate/reviewers 与 `forwin.protocol.world_v4` 已删除。 |
| `forwin.runtime_settings` | removed | `forwin.runtime.policy` | 已删除 | 不再有进程内可变生成设置文件。 |
| `Project.governance_json` settings | removed | `Project.runtime_policy_json` + version | 已删除 | 项目运行设置只由版本化 RuntimePolicy 持有。 |
| `forwin.governance*` / `models.governance` / `review.governance` | removed | `forwin.audit` + `forwin.planning` + `forwin.review` | 已删除 | 决策事件、计划控制和草稿规则分别归真实 owner，不保留兼容 facade。 |
| `api_governance_*` / `api_schema.governance` | removed | `api_project_control_*` + `api_schema.project_control` | 已删除 | HTTP transport 使用 project-control 与 audit 术语。 |
| `codex_governance` | removed | `forwin.codex_bridge.governed_actions` | 已删除 | Codex 白名单动作留在 Codex bridge 边界。 |
| `app_task_governance.js` | removed | `app_task_control.js` | 已删除 | 项目抽屉只呈现项目控制与审计状态。 |
| `forwin.orchestration` | removed | owner-local typed services | 已删除 | `ChapterPipelinePorts` / `OrchestrationEvent` 为零调用 `Any` ports，未作为 v5 边界采用。 |
| `WritingOrchestrator` + `forwin.orchestrator*` | removed | `forwin.generation.pipeline.ChapterPipeline` | 已删除 | pipeline 静态组合 typed stage owner；模块回注、身份伪装和类体函数赋值均不存在。 |
| `book_genesis.py` / `book_genesis_core` / `genesis_workspace` / `genesis_handoff` | removed | `forwin.genesis` | 已删除 | Genesis service、workspace 与 handoff 归入一个包；不再经延迟 facade 查询 helper。 |
| `api.py` 动态代理 + `api_core` | removed | `forwin.http.create_app` + instance `HttpRuntime` | 已删除 | `forwin.api` 只公开 `app/create_app/lifespan`；每个 App 独立持有 session、pipeline、task cache、scheduler 与 publisher 状态。 |
| root `api_route_registry.py` + `api_*_routes.py` | removed | `forwin.http.routes` + `forwin.http.adapters` | 已删除 | HTTP adapters 归同一 transport owner，不保留旧导入 alias。 |
| `/api/generate` + `GenerateRequest` | removed | Genesis handoff + project continuation | 已删除 | 不允许绕过 Genesis 或 durable generation task；CLI/MCP/UI 使用同一 HTTP workflow。 |
| `api_runtime.py` generic project creation runner | removed | `application.generation_execution.execute_continuation` | 已删除 | worker 只执行 project-backed claimed task；无项目 `pipeline.run()` 分支已删除。 |
| `api_project_control_ops/support` | removed | `forwin.application.project_control` | 已删除 | project-control HTTP adapter 只绑定 `ProjectControlApplicationService`。 |
| `api_project_ops` / `api_project_policy` / root `project_ops` | removed | `forwin.application.projects.ProjectApplicationService` | 已删除 | 项目、Genesis、章节和 review 路由只绑定 application service。 |
| `forwin.project_payloads` | removed | `forwin.application.read_models` | 已删除 | project summary/detail、Genesis、generation、runtime map 与 scenario 读模型归 application owner；无旧包或 re-export 壳。 |
| `api_publisher_ops` | removed | `forwin.application.publisher.PublisherApplicationService` | 已删除 | extension auth、publisher jobs、cover 与 comment sync 共享一个应用边界。 |
| `forwin.reviewer` | removed | `forwin.review` | 已删除 | 草稿评审、decision rules 与 repair 归入一个 bounded package，不留旧导入 alias。 |
| `forwin.review_engine` | removed | `forwin.review.decision` | 已删除 | 决策规则不再作为与 review 平级的第二套域。 |
| `forwin.reviser` | removed | `forwin.review.repair` | 已删除 | rewrite executor 与 verifier 归 repair owner。 |
| `HistoricalReviewHub` | removed | `forwin.review.draft_service.DraftReviewService` | 已删除 | 草稿评审只产出 evidence/verdict，不决定 canon。 |
| `FinalAcceptanceGate` | removed | `forwin.review.decision.rules.final_residual.FinalResidualPolicy` | 已删除 | repair 耗尽后的残留策略；字段为 `final_residual_decision`，仍必须经过 BookState canon commit。 |
| `_apply_world_v4_gate` | removed | `CanonPreparationService` + `CanonAdmissionService.commit_plan` | 已删除 | extraction/review 在事务外准备，compile 只在原子 Canon 事务内执行。 |
| `_compile_world_model_after_acceptance` | removed | 无 | 已删除 | 恒返回 `True` 的空壳及两处调用均删除。 |
| `WritingOrchestrator._apply_canon_candidate` | removed | `forwin.canon.CanonAdmissionService.commit_plan` | 已删除 | generation 与人工接受共享持久化 `CanonCommitPlan`；旧 `commit()` 不存在。 |
| `orchestrator_loop_core.quality_gate_types` | removed | `forwin.canon.types` | 已删除 | canon outcome 类型归 canon owner；`CanonApplyOutcome` 改名 `CanonAdmissionOutcome`。 |
| `orchestrator_loop_core.repair_loop` | removed | `forwin.review.repair.RepairService` | 已删除 | live repair 算法迁入 owner；`ChapterPipeline` 只调用 `review_candidate` / `repair_canon_block`。 |
| `orchestrator_loop_core.__init__` re-export | removed | explicit owner imports | 已删除 | `pipeline_core` 初始化为空边界；子模块不再借 `common.py` 转发外域类型。 |
| pipeline runtime helper injection | removed | owner-local typed stage owners | 已删除 | `ChapterPipeline` 构造器无 `Any` collaborator；repair/Canon preparation 只接收冻结的窄 execution context。 |
| duplicate canon quality analysis | removed | `QualityAnalysisRunRow` | 已删除 | draft review/canon gate 共享有效 primary 结果；content/plan/analyzer 指纹变化自动失效，replay/dry-run/失败结果不复用。 |
| `book_genesis_core.workflow` unreachable implementation | removed | typed `BookGenesisService` -> `GenesisWorkspaceService` methods | 已删除 | 588 行文件整段删除；handoff 锁 active revision，四个 workspace mutation 入口 fail-closed。 |
| SubWorld entity admission policy/patch/repair | removed | `EntityRegistrar` -> `EntityAdmissionPlan` -> Canon `EntityAdmissionCommitter` | 已删除 | 草稿期不写 Entity/EntityAlias；旧 canon checker、repair scope、nonblocking 例外和 summary 名字桥全部删除。 |
| `planning.future_plan_auditor` re-export + `phase24.PlanningServices` bag | removed | `PlanningService` / `PlanningQuery` / `PlanHealthService` | 已删除 | planning 包不再动态转发旧符号；实际 PlanHealth 生产调用来自 future audit，patch validation 保留自己的修订合同。 |

## 2026-07 V5 Slice 1 Status

状态：implementation-complete，已部署；旧 200 章运行已退出并降级为 pre-roadmap soak，等待新 RC gate。

- `InfrastructureConfig` 仅负责基础设施、凭据和环境模型目录；不存在 `Config` 兼容名。
- 项目运行行为只来自版本化 `RuntimePolicy`，质量 profile 仅有 `standard/pulp`，gate delegate 仅有 `human/spark`。当前契约为 `RuntimePolicy.schema_version=2`；Generation Audit 固定为 `report-only`，按数据库中 `ChapterPlan.status="accepted"` row 计数，对 `all profiles` 使用 `cadence=6`，即每六个 accepted DB chapter 记录一次 `generation_audit_checkpoint_reached`，其 `event_family="runtime_observation"`，并固定为 `no pause/delegation/block`，不改变 generation `RunResult`。
- generation task 保存不可变 policy snapshot；所有任务生产者和 worker 执行均通过 `GenerationApplicationService`。
- GET `/api/settings/llm` 是 secret-free 只读目录；控制台不再保存 API Key、模型 profile 或全局生成偏好。
- 项目抽屉是唯一 RuntimePolicy UI 写入口；MCP 对应工具为 `project_set_gate_delegate`。
- 已删除旧 mode/reckless/request override、RuntimeSettingsStore、旧治理设置 DTO，以及绑定这些接口的失效测试套件。

实现提交：`a7f53bb`、`f7790c5`、`61392af`、`6c2eb0f`、`131e697`、`bc85d91`、`3e0c10f`、`a6a75fb`、`589e58a`、`e87e67b`、`307b0dd`。

验证口径：前序聚焦 policy/store/API/snapshot/application/worker/MCP/browser 测试已通过；completion gate 的 architecture/config 为 24 passed，策略分支补充为 3 passed，`compileall` 成功，全仓 1598 tests collect 成功且无收集错误。旧长跑已退出；最终 200 章 no-hotfix 门禁必须等待新 RC 冻结后从全新项目开始。

## 2026-07 V5 Slice 2 Status

状态：implementation-complete，已部署；旧 200 章运行已退出并降级为 pre-roadmap soak，等待新 RC gate。

- 已删除零调用 `forwin.orchestration` ports 和恒成功 `_compile_world_model_after_acceptance` 空壳。
- BookState canon 旧路径已被 `CanonPreparationService` + `CanonAdmissionService.commit_plan` 取代，旧 direct commit 服务与端口均删除。
- `reviewer`、`review_engine`、`reviser` 已物理合并为 `forwin.review/{draft_service,decision,repair}`；不存在旧 package alias。
- `HistoricalReviewHub` 已破坏性改名为 `DraftReviewService`；runtime 字段为 `draft_review`。
- `FinalAcceptanceGate` 已合入 `FinalResidualPolicy`；协议/API 字段为 `final_residual_decision`，不存在旧 alias。
- `CanonAdmissionService` 已拥有唯一 candidate -> canon 原子写事务；旧 `_apply_canon_candidate`、`commit()` 和 outcome coercer 已删除。
- live repair loop 已迁入 `forwin.review.repair.service`；`ChapterPipeline` 只调用明确的 repair/canon service。
- D08 quality analysis 共享缓存已落地；旧 `WritingOrchestrator` 与 90 条跨模块方法赋值均已删除。Pipeline stage owner、`RepairExecution` 与 `CanonPreparationContext` 完成 Phase B 的行为和结构收口。
- Phase C 实现完成：Genesis handoff 永久冻结；EntityRegistrar 只产出候选 `EntityAdmissionPlan`，Canon 是唯一实体写方；旧 SubWorld admission 全链删除；planning 服务群归口到 `PlanningService` / `PlanningQuery` / `PlanHealthService`。200 章 no-hotfix 运行 gate 尚未执行。

## 已知限制

- `canon_quality` 的 form 从 BookState 读结构，倒计时与角色状态平行账本已在 P2 删除；writer ChapterWorldView 留到 P4。
- 中文否定识别目前使用轻量前缀窗口和规则 registry，能挡住明显的“不要/避免/禁止 X”误判，但不是完整句法 scope 分析。
- 质量闭环已开始从 review-time 反应式扫描前移到 plan-time patch；遗留 reviewer signal 仍会保留兜底提示，但必须避免重复注入同一约束。
- Prompt 回归测试固定 deterministic fixture 和 revision hash，不替代真实 LLM A/B 评估。

## 2026-07 V5 Slice 4 Status

状态：implementation-complete，已部署；旧 200 章运行已退出并降级为 pre-roadmap soak，等待新 RC gate。

- WriterOutput 的实体准入、状态变化、事件、剧情线 beat 与时间推进统一翻译为 GraphDelta；未知角色/事件引用与 stale old-value 均 fail-closed。
- `BookStateQuery` 已接管 context、review、checker、autofix、finalization 和 EntityRegistrar 的 accepted-state 读取；`ReviewQuery` 接管已接受摘要与 review notes。
- Canon 已删除 legacy `apply_state_changes/apply_events/apply_thread_beats/apply_time_advance` 双写；`StateRepository` 对应 accepted-state 查询族及 Genesis legacy entity 预灌已物理删除。
- `entities/entity_aliases` 只保留作 Canon 后身份唯一性索引；八张 legacy accepted-state 表及其 ORM 已物理删除。
- live page/proposal/Obsidian/retrieval helper 已迁入 `knowledge_system` / `obsidian`，`forwin.world_model` facade 与旧 world-model ORM 已物理删除。
- 项目详情、CLI、phase3/phase4、thread sampling、personality relation enrichment 与兼容 HTTP adapter 均读取 BookState/Knowledge Projection，不再回退旧 accepted-state 表。
- GraphDelta patch 持久化现在记录并按 sequence 重放，避免 create/append 顺序在数据库 round-trip 后漂移。
- Candidate 版本现在 append-only，持久化 body/plan/policy 指纹、review/repair/entity/eligibility/Canon plan 与父版本链。
- `CanonCommitPlan` 在事务外冻结；Canon 事务锁项目/章节/候选并原子提交 BookState、实体、义务、accepted 状态、审计、outbox 与 `CanonCommitRecord`。
- 五阶段故障注入、stale revalidation、幂等 replay 和 projection retry 已有聚焦测试；world edit proposal 也已归入 Canon 单写者。

Schema 同期完成破坏性收口：历史 Alembic 链与 `models/base.py` 手写升级器已删除，当时基线文件为 `0001_v5_baseline.py`（revision id 为 `0001_v5_recovery`；现 head 为 `0005_world_plan_conflicts`，见“2026-10 世界引擎大修 P3”）；生产启动拒绝不匹配的 schema，不迁移旧项目。验证口径将在本 Slice completion commit 重新记录；30/60/100/200 章 gate 均未宣称完成。

## 2026-07 V5 Slice 5 Status

状态：implementation-complete，local verification complete，已部署；旧 200-chapter run 仅保留为 pre-roadmap soak，等待新 RC gate。

- `WritingOrchestrator`、`forwin.orchestrator` 与 `orchestrator_loop_core` 已删除；`ChapterPipeline` 静态组合 12 个 typed stage owner，显式接收具体类型协作者，类体跨模块函数赋值为零。
- Genesis 已收口为 `forwin.genesis/{workspace,handoff}`；旧四个包/门面和 workspace 对 `book_genesis` 的延迟查询全部删除。
- `forwin.http.create_app()` 与 instance-owned `HttpRuntime` 已接管 FastAPI 生命周期；`api_core`、根 route registry、根 route modules、模块级 API mutable state 全部删除。
- `forwin.api` 只公开 `app/create_app/lifespan`；动态 module proxy、route handler `globals()` 注入与私有 patch point 已删除。
- `ProjectApplicationService` 接管项目、Genesis、章节和 review；`TaskApplicationService` 接管任务；`ProjectControlApplicationService` 接管 project-control；`PublisherApplicationService` 接管 publisher/extension；`GenerationApplicationService` 是唯一 durable generation task 入口。
- `/api/generate`、`GenerateRequest` 与远程 LLM-eval 调用已删除；CLI 的 generate/read/status 全部通过 `ForWinAPIClient`，worker 只调用 `execute_claimed`。
- Chapter Review API 与操作台按 `draft_review -> repair -> residual_eligibility -> gate_delegation -> canon` 五层展示；Canon 只认 candidate commit 身份，Review 详情不再使用 alert 或 `review_engine_decision`。
- 根层 `api_project_ops`、`api_project_policy`、`api_publisher_ops`、`project_ops`、`api_schemas`、`api_project_payloads` 及 context/retrieval/writer 转发壳已物理删除。
- D25 命名与所有权切换完成：audit event、planning control、review rule、Codex action、HTTP project-control 和 UI project-control 各有唯一 owner；生产 Python/JS/HTML 对 `governance` 零命中。
- 全仓生产代码不再使用星号导入、类/模块身份篡改或 `common/constants` 借道 re-export；机械删除 2,336 个未使用 import，并修复因此暴露的 8 个隐性依赖和 `llm_eval` 未定义配置。

当前验收证据：`ruff check forwin` 与 `compileall -q forwin` 通过；factory/architecture 39 项、durable task/application/worker 39 项、project operation 28 项、五层 Review 与页面渲染 14 项、Canon repair 32 项、MCP 代表路径 6 项通过；rendered inline JavaScript 通过 Node syntax check，Review 模态窗 Playwright 交互 1 项通过。内置 Browser 不可用，按批准的 fallback 使用仓库 Playwright。全仓 1565 tests collect 成功且无收集错误。代码已部署并通过生产探针；旧长跑已归档为 pre-roadmap soak，Phase E/F 仍须由冻结 RC 上的全新 200 章 no-hotfix gate 证明。

## 2026-07 V5 Post-Convergence Cleanup Status

状态：implementation-complete，聚焦验证完成；随 `master` 同步部署。

- 删除零调用根模块 `api_artifacts.py`、`api_task_history.py`，不保留 facade。
- 删除 audience 的 `analysis` 兼容 shim 与根级 `audience_metrics` 副本；信号分级、趋势、关键词情绪统一归 `forwin.audience.feedback`。
- `StateRepository` 删除 6 个零调用查询：prompt trace、repair phase、review note、全量 narrative constraint、decision event、audience trend；领域查询继续由各自 owner 提供。
- planning 删除已由 experience service 接管的 audience 查询副本和重复 calibration DTO；Writer、Review、quality gate 共享唯一 skill-layer trace 序列化函数。
- 本 Slice 生产与测试净减 734 行；`ruff check forwin`、`compileall -q forwin`、architecture boundary 27 项通过。按用户要求未重复运行全量测试。
- 第二轮残留清理删除零调用的 context/runtime/generation ports、personality validation、review interval/repair handler 与 map pathfinding facade；Genesis handoff 已接管的项目地图初始化副本及其级联 helper 同步物理删除。
- Proposal HTTP 只保留通用 `/proposals` owner，Obsidian 只保留单向 export；world-model/obsidian proposal 兼容入口、Obsidian reverse import 和 world-model Obsidian 转发入口均删除，World Studio 与 MCP 已切换到 canonical route。
- BookState path patch、quality signal 去重与数据库 retry 分类各收敛为一个实现；HTTP adapter 的 project 404 判定统一由 request support 持有。
- 第二轮生产、前端与测试净减 1,211 行；`ruff`、`compileall`、World Studio build 通过，architecture/map/proposal/DB retry/readability-continuity/HTTP factory 聚焦验证 50+ 项通过。另有 3 个 `canon_quality_service` 旧 fixture 在 `159baf8` 加入 unasked-answer fail-closed 后仍未同步，失败位于表单 schema 校验而非本轮去重路径。

## 2026-07 Integrated Roadmap Status

`docs/superpowers/specs/2026-07-06-forwin-integrated-roadmap-full-design.md` and `docs/superpowers/plans/2026-07-06-forwin-integrated-roadmap-full.md` track the integrated runtime work now reflected in code:

- Retrieval: LAN embedding gateway defaults plus accepted-chapter memory re-embedding script.
- Pulp runtime: P2 已以统一章节 delta 抽取替换旧 lightweight fallback。
- Arc planning: `ArcActivationReviewPack` and decision-event audit before continuation materialization.
- Canon / repair: `pulp_fatal` gate mode, expanded fatal signals, P0 hard obligation blocking, hard blocker detection for auto-review retry.
- Experience layer: runtime-expanded 50+ pulp trope registry, template repetition cooldown, and prompt-visible execution constraints.
- Operator UI: generation queue health summaries, review / repair queues, retry and soft-accept controls, and proposal-backed repair actions.

## 历史实施计划

| 文档 | 状态 | 说明 |
|---|---|---|
| `docs/superpowers/plans/2026-04-24-forwin-v4-world-model.md` | historical-plan | 解释 `world_model_v4` / `reviewer_v4` side-by-side 来源；已被 BookState final 覆盖。 |
| `docs/superpowers/plans/2026-04-24-forwin-v4-1-runtime-hardening.md` | historical-plan | V4.1 hardening 计划；“V4 source semantics” 口径已被 BookState final 覆盖。 |
| `docs/superpowers/specs/2026-07-09-forwin-reckless-review-mode-design.md` | historical-plan | 已被 RuntimePolicy `gate_delegate=human|spark` 与 fail-closed gate delegation 覆盖。 |
| `docs/superpowers/plans/2026-07-09-forwin-reckless-review-mode.md` | historical-plan | 独立 reckless mode 已删除，不再是当前产品或运行时概念。 |
| `forwin_design_cleanup_update_plan.md` | historical-plan | 旧冗余清单，已被 v5 审计和实施规格覆盖。 |
| `forwin_decoupling_plan.md` | historical-plan | 旧解耦建议；其中 `forwin.orchestration` 目标已被 v5 owner-local service 设计取代。 |
| `pulp_profile_upgrade_plan.md` | historical-plan | 旧实现计划；有效运行语义已固化进 `RuntimePolicy.for_profile("pulp")` 与当前架构入口。 |

## Future Product Backlog

以下内容不作为当前 V4.5.x 后端缺口：完整 World Studio 图谱/地图/认知 UI、metrics dashboard / SLO 看板、Skill API / UI / script-backed / tool-backed execution、native GraphDelta extractor、完整 world/map/cognition rule pack、复杂交通工具体系、可视化地图编辑器、Neo4j 主存储、tile renderer、Genesis 深层 workflow editor、自动 retcon accepted canon、完整 LLM editorial reviewer 产品化。
