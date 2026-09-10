# Stage 3 有限反馈证据链

日期：2026-09-09。依据已批准的[三阶段设计](../specs/2026-09-09-forwin-three-stage-design.md)第 6 节。此次验证使用当前源码 checkout、隔离 PostgreSQL 和冻结模型响应；不连接真实模型或发布平台。

## 已验证的边界

- `FeedbackPlanService` 只消费唯一 ActionMapper 的已选、来源合格行动；只追加三个允许的体验计划字段。真实草稿、候选、Canon、发布事实、同章 generation reservation / 在写任务都会阻止修改。Project→ChapterPlan 锁、`candidate_plan_revision` CAS、before/after 完整小计划与来源审计在同一事务；占用导致 deferred，可在占用解除后重试。
- 所有现有体验计划持久化入口使用捕获版本与 Project→ChapterPlan CAS。Band 在任何 activation / schedule 写入前检查整组计划，整个写包使用 savepoint。独立 PG 反例曾证明旧 typed payload 可以覆盖新反馈提示；修复后明确拒绝，计划与应用审计不再分离。
- `FeedbackBodyObservationService` 只写行动的 `body_observation_json`，不改 Canon，不调用模型，不增加正文门。它核对 Canon→Candidate→Draft 的正文 hash、该提交的冻结计划身份，以及候选 metadata、行动 inclusion、真实 PromptTrace 三方相同的 attempted input。
- 自动观察始终是 `unknown`。明确 `observed / not_observed` 必须提供 human 或 frozen observer 来源、完整正文 hash、说明与逐字字符区间引用。引用验证只证明来自该正文，语义判断仍归记录的观察者。缺少旧来源或计划版本不会拿当前 ChapterPlan 补造。
- 原始 unknown、正向和负向观察追加保留。effects 对同一 Canon / BODY 的相反评估返回证据不足，不挑选正向项；结果始终 `causal_claim=false`。

## 有限样本

`tests/test_feedback_finite_loop.py` 通过真实接收、分析完成记录、聚合、ActionMapper、post-Canon feedback step、计划 owner、Writer、PromptTrace、CandidateDraftRepository、CanonPreparationService 与唯一 CanonAdmissionService 重放。

| 样本 | 实际输入与结果 |
| --- | --- |
| 正向证据链 | 第 2 章的 3 位作者各一条风险评论，实际聚合后选择行动并修改第 3 章计划。第 3 章经过真实 Writer 调用边界（冻结响应）、trace、候选持久化与 Canon 原子接纳。独立标签引用正文的“归还钥匙才开门”句，绑定相同计划 revision、输入 ID、trace、Canon 与 BODY hash。 |
| 后续关联 | 普通接纳推进到第 5 章，使既有短窗 `[3,5]` 与基线 `[1,2]` 分离。第 3 章的 4 条后续评论有 2 条同向命中，完整消费包括两条零信号结果；相同真实 publication / Canon / hash 的聚合得到占比 `1.0 → 0.5`、`associated_desired_change`。这是描述性关联。 |
| 低置信 | 3 条独立评论的冻结分析置信度为 0.3；未来计划保持原样，不产生执行性行动。 |
| 单作者 | 同一作者重复 3 条严重风险。此样本先复现了 watchlist 被直接应用到未来计划的错误；修复后保留 `watchlist_observation`，无 `plan_hint`，Writer 明确沿既定计划继续，计划应用拒绝。 |
| 相反强方向 | 两个已发布章节各 4 条评论，快 / 慢方向各有 4 位作者且跨两章，确实达到现有聚合确认条件。两个方向均保留为 `conflicting_directions` 观察，无改纲提示，计划不变。 |
| 迟到 | 第 3 章接纳后收到第 2 章的 3 条新评论。来源仍绑定第 2 章原发布 Canon；旧行动冻结聚合证据、已接纳正文与计划身份保持，任何新计划应用只能指向未来章。 |

正向样本的 5 个 Canon 记录全部由 `CanonAdmissionService.commit_plan` 创建。第 1、2、4、5 章的 WriterOutput、质量批准 / GraphDelta 输入、其他 post-Canon barrier 完成事实以及远端已发布事实是明确的测试夹具；第 3 章才执行真实 Writer owner 的冻结 transport。此测试没有替代真正语义评审、真实平台发布或全量连载验收，也不声称模型能力、实际 token 节省、阅读量或留存提升。

## 验证

```sh
.venv/bin/python -m pytest -q \
  tests/test_feedback_finite_loop.py \
  tests/test_feedback_body_observation.py \
  tests/test_feedback_plan_service.py \
  tests/test_feedback_integration.py \
  tests/test_experience_plan_cas.py
```

结果：**67 passed / 6.03s**。其中完整有限样本 5 项，BODY owner 27 项，计划 owner 26 项；另包含 post-Canon 接线与真实 PG CAS 回归。新文件 Ruff 检查及 `git diff --check` 通过。此次结果证明这些有限接口与证据链，不代替 Stage 1 的冻结 smoke / 全新离线 L100，也不代表全部 Stage 3 运维验收已完成。
