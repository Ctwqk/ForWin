# Stage 3 行动与 Writer 输入证据

本包实现批准路线图 P2-2 的行动映射、选择和实际输入证据。窗口、等级、作者与来源资格仍只由 `audience/aggregation.py` 计算；计划修改、正文观察与后续信号变化由各自 owner 使用本包保留的证据字段。本包不修改质量、长度或修复预算。

## 一个映射与一种提示

`ActionMapper` 只消费 canonical aggregate view，要求冻结的 aggregate identity、version、evidence hash、source qualification 及已计算等级。六类信号按闭集方向产生不同建议：快与慢、正负角色热度、增减关系关注分开；unknown 不自动猜方向。prediction 有 Writer 观察提示，保留预测目标，明确不要求迎合或刻意反转，也不产生自动计划命令。

`action_payload_json` 保存 Writer hint、可选有限 `plan_hint` 和 `desired_signal_change`。后续 owner 不再根据 signal type 复制一套映射。`aggregate_evidence_json` 保留完整冻结来源与版本；Writer 只得到安全建议和行动 ID，不得到原始评论或读者身份。

`AudienceHintView.items` 是唯一写入表示，旧分类字符串列表是派生只读视图，新增 prediction 类别。StateRepository 只返回 qualified、selected、当前有效的 items；每类至多三条。Provider 启用由集成包负责，不恢复旧全局规则或第二套 audience calibration。

## 独立生命周期

- proposal 使用 project / aggregate / action 的确定身份，重跑不会再建一条记录。只有明确选择才设 `selected_at`、`selected_at_chapter` 并启动冷却。
- 响应范围沿用六类原有章数：risk 3、pacing 5、confusion / character / relationship 10、prediction 6。
- 提示有效期为提议之后三章；有效期与响应范围、默认三章冷却分别保存。延迟选择不让提示出现在选择之前的章节，也不延长原有效期。
- 裁剪未选中的行动保持 proposed，不启动冷却。选用、进入实际输入、进入计划、正文落实、后续观察不是同一状态。
- `plan_application_json`、`body_observation_json`、`effect_observation_json` 初值均为空对象，单纯输入提示不写成功值。

## 实际模型输入与事务

提示渲染使用 `action_id + 完整文本 hash` 标记。ChapterWriter 在最终 `llm_client.chat(messages, ...)` 边界校验实际消息里的完整标记和文本，记录每次 input ID、消息 SHA-256、stage 与保留的行动 ID。相同文案但不同 ID 不会使被裁剪行动误获证据。BODY parser 拒绝提示标记泄漏。

状态称为 `attempted_input`。adapter 返回和 adapter 抛异常分别保存 returned / raised；这不证明远端 provider 成功、exactly-once 或正文效果。失败仍保留原异常，不改既有模型、重试或场景拆分语义。历史修订的浅拷贝 Writer 使用独立输入缓冲，不污染普通 Writer。

成功 trace 仍在既有 Review/Repair 保存；失败 trace 仍由 WriterExecutionTelemetry 保存。实际 inputs 位于 PromptTrace.input_snapshot_json 及 WriterOutput.generation_meta 的同名 trace 中；candidate metadata 沿现有持久化路径保留这些 input IDs。行动记录的 prompt_inclusions_json 引用真实 trace / input / chapter / message hash，供后续 owner 精确关联最终 candidate。

有反馈输入时先锁 Project，再保存 PromptTrace 和 Action 关联。它们使用同一调用者事务：commit 后才持久，rollback 一起撤销；未提交前的进程崩溃没有虚构持久证据。本包不声称 LLM 调用能随数据库事务回滚。

## 向前迁移

`0007_feedback_actions` 接在 `0006_feedback_aggregation` 后，仅扩充现有 FeedbackActionRecord，不新建控制层或历史副本。旧记录全部原值保留，新资格默认为 false、状态 proposed、版本/输入/观察证据为空，旧 cooldown 不被解释为新版本的已选用行为。已有新生命周期证据时拒绝降级丢弃。

## 验证

新回归覆盖方向与 qualification、prediction 实际输入、裁剪同文案、成功及 BaseException、trace/行动同事务回滚、真实 Scene 调用、提示标记泄漏、独立修订 Writer、幂等提议、延迟选择、真实 PostgreSQL 并发选择、Project 先于 trace FK 写入，以及真实 0006→0007 数据保留迁移。

首次整合检查 13 个相关文件 141 项通过；随后补充延迟选择的 RED→GREEN 窄回归。完整仓库最终验收、自动消费 / provider / planning / body / effect 闭环由集成步骤单独记录，不能由本包单独代替。

## 独立审查修复

同目标、同类型且来源章窗相交的相反 confirmed 方向，Mapper 统一保留为 `observe_conflicting_directions`。不按 short / medium / long 名称绕开冲突，也不重算原等级或挑严重者赢。两个 aggregate 的 identity / version / hash 随决定保留，`plan_hint` 为空，期望变化为 observe。实际 aggregation-pass 将全部窗口交给 Mapper，冷却仅在后续 selection 处理，避免丢失已冷却的相反证据；已写计划不自动回滚。

坏的历史 JSON 被视为不具资格并保留原文。冻结 snapshot 的 project / signal key / signal type / direction 必须与行动一致，另一项目的 aggregate 不能经 record_actions 写入本项目。上述审查负例先失败后通过，包含真实 aggregation-pass 的冷却冲突接线。
