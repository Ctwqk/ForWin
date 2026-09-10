# Genesis 路线字段合同修复

冻结候选 `5524e362ed757a270801dd560d1984d185e7c7eb` 的完整回归为 **3156 passed / 4 skipped**，运行和浏览器镜像均通过完整验证。但新项目 `9a5fc557a23647c6be063366a08815c2` 在 Map revision 7 的写前检查中暴露字段合同缺口，已停留在 Map 锁定之前，未创建章节任务。它没有通过 smoke，也没有产生可用于判定正文失败的章节。

## 复现与修复

该 Map 的八条路线用 `travel_time` 字符串表达耗时、`constraints` 列表表达十三项通行条件、`mode` 表达八种交通方式。旧生成端没有声明边的字段 schema，消费方只理解另一组字段。对原始 canonical read 做冻结源码纯函数重放，八条耗时、十三项条件和八种交通方式都未进入运行输入；八条风险说明仍保留。这是生产者与消费者的合同缺口，不能归因于模型违反已有 schema。

现在由 `forwin.map.genesis_route` 提供唯一 typed route 和 parser，生成 schema、修订/patch/锁定校验、预览与导入共同使用。新完整 Map 必须声明 `edges`，允许空列表；新路线使用 `duration_text / mode / conditions / risks` 等明确字段。旧格式按有限别名解释，原始 revision 字典不改写，原始路线和字段路径进入来源 metadata。

数值旧 `travel_time` 仍以小时计，完整带单位文本使用既有时长解析。范围和约数不被折算成中点；并存且不一致或不可比较的耗时来源拒绝。非耗时费用文案仍保留。未知字段、错误类型、重复身份及无法解析的显式路线不能被静默过滤或替换成程序化默认地图。

节点引用与父 SubWorld 引用分开解释，导入时核对归属。SubWorld 级路线也保留独立身份、类型、耗时、通行条件及显隐状态，不合并平行路线。预览在解析后再次执行原可见性检查，避免旧 `type="hidden_route"` 别名泄露条件。自然语言许可与风险仍不代表人物已获许可或事件已经发生；程序化默认连接、既有 Canon 与质量门不因此改变，已部署地图不会自动重建。

## 验证与边界

- 新回归覆盖五种 Writer 输入、小时与文本单位、范围/冲突、原文保留、坏类型、缺失/空/损坏路线、隐藏边、平行跨区路线以及 PostgreSQL 持久化。
- 实际 patch、完整/定向 refine、Map lock 失败后，active revision 与原始字典保持不变。旧无效 revision 仍可读取，handoff 拒绝且不留下部分 Arc、ChapterPlan、任务或地图。
- 根代理的路线、Map、Genesis workspace/handoff/flow 组合回归 **144 passed**；独立复审与相关路线回归 **98 passed**（有重叠），未发现新阻断问题；这是相关测试，不能代替全量回归。
- 同一份真实 Map 在修复后的 owner 重放中保留 **8/8 耗时、13/13 条件、8/8 交通方式、8/8 风险**，每条来源 metadata 与原路线对应。该重放不调用 LLM、不操作业务数据库，不能证明模型最终遵守这些条件。

私有证据在 `.superpowers/sdd/2026-09-09-forwin-three-stage/runs/forwin-stage1-smoke-5524e362ed75-f807dc/map-edge-contract-diagnosis/`，原 canonical read SHA-256 为 `b6727c699af348f6c1245b8eb39ad0eda0420d60b527467d68d1d1f020bdfc01`。`before.json` 保留冻结旧源码结果，`after-working-tree.json` 保留修复重放，当前源码、测试及独立审查状态以[执行计划](../plans/2026-09-09-forwin-three-stage.md)和运行证据为准。

新候选还需要独立冻结后的全量 QA、两类角色镜像、全新 smoke20 和另建的独立 L100，包括真实结局目标审读。没有重写旧失败样本、手工放行、降低质量或部署到生产。
