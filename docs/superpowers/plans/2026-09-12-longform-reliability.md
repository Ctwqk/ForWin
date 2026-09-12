# 长篇生产可靠性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 消除义务无依据清账、批次交接丢失、失效派生事实进入 Writer，并补齐有限修复和必需上下文。

**Architecture:** 复用 Canon 接纳事务、持久 task/outbox、ChapterReviewForm、BookStateQuery 和 RetrievalBroker。派生存储不获得正式状态写权；替代实现接入后删除旧决策路径。

**Tech Stack:** Python、SQLAlchemy、PostgreSQL、Pydantic、Alembic、Qdrant、现有 ModelAdapter 与 pytest。

**Spec:** `docs/superpowers/specs/2026-09-12-longform-reliability-design.md`。

## Global Constraints

- 只在 `/Users/magi1/ForWin-source-github` 编码；继续现有 `codex/three-stage-improvements` 分支，起点 `f5dec620`，上游 `origin/for-review`。不触碰现有未跟踪文件。
- 当前用户已要求按四包建议规划并开始，不再要求第二次设计/执行方式确认。逐任务实现及独立审查，禁止多个实现者同时修改共享源码。
- 真实生产项目/任务/章节读取使用 ForWin MCP；本计划测试使用隔离 PostgreSQL，不修改旧作品。
- 前向迁移，不改 deployed baseline。保持冻结、5%、offline、完整后缀、lease 和修复预算。不得用新旧生产源码拼接构建。
- 测试命令前缀：`FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test .venv/bin/python -m pytest`；tests.postgres 自动创建独立临时库。
- 精确例子/现有测试 fixture 是实施起点；不为维护旧错误行为保留旁路，不把 fake model 的正确回答当成真实模型质量证明。
- 新模型调用、整书长跑、部署资格沿原三阶段计划；本轮不启动定时续跑、不公开发布、不自动修旧数据。

## Task 1: A1 — 用共同审阅证据原子兑现义务

**Files:**
- Create: `forwin/narrative_obligations/resolution_evidence.py`, `tests/test_obligation_resolution_evidence.py`.
- Modify: `forwin/narrative_obligations/candidate.py`, `forwin/canon_quality/obligation_verifier.py`, `forwin/canon_quality/chapter_review_form/{form_schema,form_builder,service,llm_caller}.py`, `forwin/canon_quality/{types,service,cache}.py`, `forwin/canon/{types,plan,preparation,quality_preparation,admission}.py`, `forwin/generation/pipeline_core/obligation_resolution.py`.
- Test: existing obligation verifier, acceptance history, historical form, Canon atomic/preparation and quality-cache suites.

**Interfaces:** 新增不可变 `ObligationResolutionEvidence` 与 `ObligationResolutionPlan`；证据包含 obligation_id、contract_fingerprint、draft_id、body_sha256、审阅来源、判断、答案及精确 body 引用。`CanonQualityGateOutcome` 携带该计划，`CanonPreparationService.prepare_from_approved()` 的可选参数写入 `CanonCommitPlan` 并参与幂等身份。共同 validator 接收实际义务/form/answers/body；历史 wrapper 保留六维覆盖，普通路径不调用整个历史核验。

- [x] RED：在现有真实 PostgreSQL obligation fixture 中以三种正文调用现有接纳后维护，断言不能 resolved；未来到期义务同样覆盖。

```python
@pytest.mark.parametrize(('kind', 'body'), [
    ('motivation_gap', '因为下雨，他在客栈住了一夜。'),
    ('identity_ambiguity', '刺客的身份仍然不明，没有找到证据。'),
    ('custom_reader_promise', '他关灯睡了。'),
])
def test_unreviewed_body_never_resolves_active_obligation(kind, body):
    engine = get_engine(postgres_test_url('unreviewed_resolution'))
    init_db(engine)
    factory = get_session_factory(engine)
    try:
        with factory.begin() as session:
            project = Project(title='义务反例', premise='测试', genre='悬疑',
                              target_total_chapters=20)
            session.add(project)
            session.flush()
            obligation = NarrativeObligationRepository(session).create_obligation(
                _obligation(project.id).model_copy(update={'obligation_type': kind})
            )
            verify_active_obligations_after_acceptance(
                session=session, project_id=project.id,
                chapter_number=11, accepted_text=body,
            )
            obligation_id = obligation.id
        with factory() as session:
            assert session.get(NarrativeObligationRow, obligation_id).status == 'active'
    finally:
        engine.dispose()
```

代码放入已有 `test_obligation_resolution_verifier.py`，复用其 import 和 `_obligation`，添加 pytest import。后续删除被替代入口时，将此反例迁移到维护的正式入口。另覆盖无关人物、未来承诺、旧候选引用、条件核验后变化；真实完整兑现、合法提前兑现及事务中途 rollback 为正反对照。

- [x] Run: `pytest -q tests/test_obligation_resolution_verifier.py tests/test_obligation_resolution_evidence.py`（使用 Global Constraints 前缀）；保存业务断言失败证据，不把建库/导入错误当 RED。
- [x] 实现同一审阅证据传递：保留 form/answers/validation report 在 quality result/cache；form ask 携带主体、条件和完整合同指纹，现有语义 prompt 明确区分已兑现/未兑现/无法确认。缓存版本升级，旧缓存不得伪造证据。未知旧类型不默认 pass，关键词/marker 不再拥有清账权。
- [x] 在 preparation 冻结证据及依赖，admission 锁内重读义务完整合同并复核候选 body/draft/审阅身份，激活后和正式 active pointer 同事务应用已证实兑现；提供现有 failure injection 的 rollback 证明。空证据保持未解决，不能获得到期豁免；idempotent 已提交读取仍保持兼容。
- [x] 抽取历史 `apply_reviewed_resolutions()` 的通用部分，保留其 before-image/全覆盖规则；删除普通接纳后的重新语义判断，只保留已提交结果读取和到期维护。
- [x] GREEN + related: verifier/evidence/history/historical form/Canon atomic/preparation/quality cache。证明旧 resolved 行不会自动回填或重置；新记录保留可审计的来源。实际旧书修复前另作有范围的只读审计，本任务不新增无人调用的审计服务。
- [x] 同步当前设计的已实现范围并提交 `fix(canon): resolve obligations from version-bound review evidence`；独立任务审查。

## Task 2: B — 持久、原子的续跑交接

**Files:** Create `forwin/generation/continuation_events.py`, `tests/test_generation_continuation_outbox.py`; modify `forwin/application/{generation,generation_execution}.py`, `forwin/generation/{auto_continue,task_repository}.py`, `forwin/models/task.py`, `forwin/outbox/handlers.py`, `forwin/http/project_support.py` (task pause/cancel owner), new forward migration after current Alembic head. Verify existing `forwin/generation/task_payload.py` frozen fields and `forwin/runtime/container.py` handler integration; no edit required if the existing contract is sufficient.

**Interfaces:** `GenerationApplicationService.finish_claimed_task(task_id, result, *, worker_id, lease_epoch)` owns finalization; deterministic `generation-continuation:{parent_task_id}:v1` event carries typed result. Consumer uses a caller-owned Session; `GenerationTask.continuation_parent_task_id` is nullable and unique; deterministic `AUTO_CONTINUE_DECISION` audit ID persists child-or-stop decision. Parent frozen task payload supplies policy and run scope.

- [x] RED：真实 application/worker tests 覆盖完成事务失败、完成后消费前进程退出、子任务提交后 ack 丢失、子任务完成/软删除后重复投递、双 consumer、暂停/取消并发、review reset 后入队失败、变更项目策略、恢复路径及 stale lease。
- [x] Run new suite and existing `tests/test_generation_application_service.py tests/test_generation_worker_canon_recovery.py tests/test_generation_auto_continue.py`，确认交接丢失/重复创建或策略漂移的失败。
- [x] 前向迁移增加可空唯一父任务字段，旧行保持 NULL；新增事件 schema/handler，不建第二调度器或 intent table。
- [x] 最终任务状态与 event 在一个 fenced transaction 写入；所有完成路径共用入口；进度回调只写进度，移除 best-effort completion callback。已提交完成后的显示错误不回改结果。
- [x] 消费按 Project → GenerationTask 锁定，原子重查控制/维护/目标/容量/其他活跃任务，创建子任务与 review reset、audit 决定同事务。未消费 completed 父任务可暂停该意图；竞争失败不声称已经暂停。旧事件遇新运行记录 superseded。
- [x] 子任务复用父 policy_snapshot/policy_version/run_until_chapter/long_run_mode/isolated，仅更新身份批次；若项目策略版本已被用户修改，旧意图记录 policy_changed 并停止，不伪造新版本或弱化 Canon stale 检查。用例断言旧快照未被改写、无旧策略冒充新版本的子任务；新设置由显式新运行采用。数据库唯一键和既定 audit 决定处理全部 replay。
- [x] GREEN：上述 suite、outbox worker/lease/laziness、capacity、migration preservation；提交 `fix(generation): persist idempotent continuation intent with task completion` 并审查。

## Task 3: A2 — 义务阻塞接入有限修复

**Files:** Modify `forwin/canon_quality/{gate,signals}.py`, `forwin/canon/{quality_preparation,preparation,admission}.py`, `forwin/narrative_obligations/{repository,resolution_evidence}.py`, `forwin/candidate_drafts.py`, `forwin/application/projects/reviews.py`, `forwin/generation/pipeline_core/project_chapters.py`, `forwin/review/{repair_scope_router,candidate}.py`, `forwin/generation/review_auto_retry.py`, `forwin/review/repair/service.py`. Verify existing `forwin/review/repair/plan_patch.py` supported scopes and history protection; no no-op edit required. Test `tests/test_canon_admission_gate.py`, obligation repair and pipeline tests.

**Interfaces:** 扩展已有 `CanonAdmissionGateResult` 的结构化阻塞项，保留 blocking_reasons 作为显示；每项含 obligation_id、未满足条件、来源、scope、failure domain。Task 1 的 reviewed resolutions 决定到期是否清账，不能重新启用关键词。

- [x] RED：构造仅到期未兑现、signals=[] 的候选，实际流水线应使用一次章修复；修复新候选通过后才接纳。预算耗尽、基础设施失败、没有 executor、冻结历史分别停止且不延期/waive。
- [x] 用已有 route/contract 接收 typed blocker；Writer 获得具体义务和 must_preserve；scope 不可执行给出确定结果，不在流水线新增字符串匹配链。
- [x] admission 专用查询保留 active/expired/blocked 的有效来源义务，Writer/form 仍只选 active；冻结依赖和锁内重查一致。覆盖准备后状态变化，严格保留 strict 与 pulp P0/hard-only 策略，不能让过期债因查询排除而消失。
- [x] 复用同一 repair-cycle 累计预算，新候选重建 review/eligibility/approval/Canon evidence；新 task 不绕过已耗尽预算。review/Canon phase 保留审计标签，但不各自重获一套额度；更新旧的 `test_canon_repair_budget_ignores_prior_review_repair_attempts` 反向契约，沿用现有 effective rewrite limit。
- [x] 跑实际流水线/repair/obligation regression，提交 `fix(review): route obligation blockers through bounded repair` 并审查。

## Task 4: C1 — 固定读取基线并拒绝失效派生事实

**Files:** Create `forwin/retrieval/source_identity.py`, `forwin/knowledge_system/dependencies.py`, `forwin/llm_kb/source_validation.py`; modify `forwin/retrieval/{memory_index,broker_core/broker}.py`, `forwin/protocol/context.py`, `forwin/book_state/query.py`, `forwin/context/request.py`, `forwin/context/assembler_core/assembler.py`, relevant `forwin/context/providers/`, `forwin/knowledge_system/{context,page_repository,store,canon_projection}.py`, `forwin/obsidian/exporter.py`, `forwin/models/knowledge.py`, `forwin/llm_kb/{compiler,vector_index,retriever}.py`, new forward migration. Tests: `tests/test_memory_index_embedding.py`, `tests/test_knowledge_system_v46.py`, broker and prompt regressions.

**Interfaces:** 不可变 `CanonReadBaseline(project_id, book_revision, as_of_chapter)`；batch active-source selector；纯函数语义 page dependency fingerprints。MemorySnippet 增加来源 ID/hash/embedding 身份，旧字段默认空且不具有效性。broker context 在基线变化时最多重建一次。

- [x] RED：修订已接纳但投影暂停，最终 Writer messages 不得含旧状态/摘要；旧 worker 晚到、伪造 hash、unknown legacy、未来 top hits、有效低排名补检和新增章节仍保留未变历史。
- [x] 基线覆盖 assembler/BookState cache/cognition/pages/vector/备用知识加载；缓存增加 book revision，整包前后 fresh 校验，不吞基线错误。
- [x] 向量来源经 active commit→candidate→draft 批量核对并用正式文本构造片段；immutable point identity 防旧写覆盖，分页每批最多20、最多5批、query 只 embedding 一次，记录拒绝数及上限到达。
- [x] 页面依赖覆盖实际字段、状态、关联集合及聚合输入；验证在排序/限额前。Obsidian/LLM-KB 的文件和检索结果同样验证，无法证明的旧内容暂不进入 Writer；当前必须事实仍从 BookState 获取。
- [x] migration 不改旧 Markdown/hash/notes，unknown dependencies 不填造。测试新增/删除关联、same-height revision、chapter-zero world edit、late events 和所有 secondary loaders。
- [x] 跑相关 suite 和实际 prompt regression，提交 `fix(retrieval): validate derived context against active Canon sources` 并审查。

审查遗留（P3，终审处理）：同一有效知识库段落的多个 embedding 版本可能重复占用结果 limit；需按已验证段落去重并保留有界补检，或显式选择当前 embedding 版本。未发现该情形引入旧事实。

## Task 5: D1 — 接通已接纳人物与读者认知

**Files:** Modify `forwin/book_state/query.py`, `forwin/protocol/context.py`, `forwin/context/providers/`, `forwin/context/assembler_core/assembler.py`, `forwin/retrieval/broker_core/broker.py`, `forwin/writer/prompt_core/sections.py`, `forwin/review/{context_builder,webnovel,llm_webnovel}.py`.

**Interfaces:** 同一 Task 4 baseline 的 typed accepted cognition snapshot，含 observer ID/type、显式 ref 状态、错误认知/overrides、证据和 as-of。已存在计划 intent 字段保留计划意义，不能填 current reader/observer fields。

- [ ] RED：N−1 A知/B未知/读者只见线索，N后半才告知B；读取 N−1 的 field patch、false belief 和 override；五种布局+repair实际发送请求里 B 不提前知情。
- [ ] 从 CognitionView 明确字段读取，get_belief absent 保持 unknown，不用 can_see 默认值及旧 overlay 副本；assembler 和 broker merge 移除计划 truthy fallback。
- [ ] Writer/Reviewer 分开渲染已接纳认知、作者计划、预期本章变化及带条件场景承接；秘密可以存在于作者区域，但不成为角色知识。
- [ ] 跑 BookState/provider/prompt/review regressions，提交 `fix(context): separate accepted cognition from planned reveals` 并审查。

## Task 6: D2 — 必需对象与当前状态贯穿实际请求

**Files:** Modify `forwin/retrieval/broker_core/broker.py`, `forwin/book_state/query.py`, `forwin/protocol/context.py`, `forwin/context/assembler_core/canon_quality_context.py`, `forwin/writer/prompt_core/{builders,sections}.py`, `forwin/review/repair/plan_patch.py`, `forwin/writer/llm/errors.py`, relevant actual request error handlers.

**Interfaces:** ChapterContextPack 保存 required_entity_ids/required_relation_ids 和选择来源；RelationSnapshot 含 edge/endpoints/state；统一 requirement resolver 区分 ID、唯一别名、node/field/edge/fact refs及非实体任务类别。软预算元数据与真实 provider input_limit 明确区分。

- [ ] RED：超过10个必需低排名人物、义务唯一引用、别名歧义、必须关系、修复引入原包未选人物；检查全部实际 Writer messages 的身份/状态/名单与关系，场景换序或高排名人物增加不挤掉核心角色。
- [ ] 保留 obligation subject_refs，先解析必需再补可选。有限 soft trim 只能删除可选并压缩背景，required关键状态不丢；下游 caps只作用可选，允许名单不显示截断样本冒充完整。
- [ ] RepairPlanPatchService 在新目标后按同一 accepted baseline rehydrate，不能只重裁旧包。歧义/真正缺失的必需事实明确输入失败。
- [ ] 输出真实渲染 soft_budget_exceeded 统计；provider input_limit 明确不重试相同输入，不新造字符→token硬门。
- [ ] 跑 context budget/prompt contract/snapshots/repair/LLM retry，提交 `fix(writer): preserve required entities and state across prompt layouts` 并审查。

## Task 7: C2 — 增量投影及可恢复 embedding 缓存

**Files:** Modify `forwin/knowledge_system/{canon_projection,checkpoints}.py`, `forwin/models/{projection,canon}.py`, `forwin/retrieval/memory_index.py` (includes GatewayTextEmbedder), `scripts/reembed_memory_index.py`; create embedding cache model and forward migration. Tests: `tests/test_memory_index_embedding.py`, `tests/test_reembed_memory_index.py`, `tests/test_projection_checkpoints.py` and migration regressions.

**Interfaces:** checkpoint target/projected book_revision 按既有 fencing推进；以 CanonCommitRecord.base_book_revision 区间和 active pointers 确定变化集合。durable embedding cache key = actual input hash + model/backend identity + dimensions + preprocessing version；向量空间按模型身份隔离。

- [ ] RED：接纳100章只读取/嵌入第100章；相同事件重放0新增embedding；修订后缀身份变但输入未变复用缓存；同维度换模型不复用；reembed有较新未接纳稿仍只读取active稿。
- [ ] checkpoint支持同章新revision/chapter0；old valid superseded event安全收敛或no-op，不无限retry；合并事件覆盖整个缺失revision区间，旧ticket不回退新checkpoint。
- [ ] 普通路径只取变化正文；unknown迁移checkpoint明确一次重建。缓存校验维度/有限数值，模型身份无法确认不得以维度猜测。全量工具复用同一active selector与cache。
- [ ] 跑 embedding/reembed/projection/outbox/migration suites，提交 `perf(projection): index changed Canon sources and reuse versioned embeddings` 并审查。

## Task 8: 集成核验与交付

**Files:** 当前设计、上面补充设计/计划、简短实施报告；本计划独立 SDD ledger/evidence。

- [ ] 冻结集成源码，跑 `.venv/bin/python -m pytest -q`、compileall、修改文件 F/E9、source closure guard、diff检查和前向迁移回归；全量 Ruff记录新增问题，不能隐瞒既有基线。
- [ ] 用 fresh reviewer检查完整diff的规格覆盖、原子性、并发、失效读路径、实际prompt终点；修复并定向复核。
- [ ] 更新已完成/待实跑边界，按已有授权提交并push `origin/for-review`，fetch验证0/0。生产同步前仍须完整镜像/旧库前向迁移验证，不能跨越既有未通过资格。
- [ ] 继续现有阶段候选的smoke20→独立L100→结局审读；如凭据/配额尚无变化，保留明确阻塞和源码身份，不创建定时任务、不假装通过。
