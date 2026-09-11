# Stage 2 A1/A2 离线消融结论

A0 后冻结源码：`59b2bbbe91ef5c264eca8f49ec73213756c69084`。本次正式重放沿用 A0 前的 9 条 `stage + SHA256(messages, options)` 响应及独立合同/正文标签，仅将脚本连接到新 `RepairVerifier` → `merge_repair_verification` 接口。9 个输入键全部命中，5 项重放保护测试通过；所有 A1/A2 业务结果与重构前相同。没有调用真实模型，也没有修改生产输入、策略或质量门。

| 项目 | 固定对照及实际测量 | 结论 |
| --- | --- | --- |
| A1：主评审通过后，语义修复合同核验是否重复 | 3 个样本；baseline 实际执行 5 次离线 adapter 调用，共 16,641 输入字符。只关闭语义 verifier 后为 0 次；中部秘密泄漏与抛弃承诺两个已知严重负例从明确 fail 变为主评审 pass、合同 unknown。 | **保留。** 两个反例证明此检查有独立职责，删除候选被拒绝。 |
| A2：single Writer 的重复 Canon 约束块 | 同一 user message 的第二份逐字相同约束块只在候选内存输入中删除。倒计时样本 1,861→1,409 字符；死亡角色旧档案样本 1,125→1,041 字符。每版本每样本各 1 次离线 adapter 调用，唯一约束行均保留，解析 BODY 哈希相同。 | **证据不足，保留现有实现并结束本候选。** 固定响应一致不能证明真实生成质量不退步；该 single 路径也不能外推到主 scene Writer 的收益。 |

A1 独立标签来自明确合同与完整正文引用：禁止揭露王后身份的合同，对照修复稿 offset 1500 的“王后就是叛徒。”；保留陪伴承诺的合同，对照“她答应留下。”与“她抛下同伴离去。”。另有“旧错误”→“已纠正”的极简正控制。A2 使用现有 `cross_chapter_countdown_regression` 与 `already_dead_character_mentioned_without_resurrection` 正文 fixture：无重置依据的 10→20 分钟为局部事实失败；仅提亡者档案、不见其行动，不等于复活。旧 reviewer 输出不是唯一标签。

这些是受控反例及输入测量，不是模型发现概率或现实漏检率：A1 主评审 pass 是固定输入，未重跑整个 DraftReview；A2 没有实际语义 gate，严重漏检记为“未测量”，不能记作零。Provider usage 不存在，实际 token、延迟、修复总成本、人工介入、文学质量及盲评均未测量；不报告 token、金额或 15% 上线收益。没有由此启动真实模型 18 章对照。

A3 只保留消费者调查，不改变维护频率：

| 维护产物 | 实际下游 | 尚缺的版本证明 |
| --- | --- | --- |
| planning 的阶段分析与未来计划修改 | 同步 ReplanGovernor、后章 ExperienceContextProvider/Writer 合同 | 下一章消费已完成 replan 对应的有效计划身份 |
| arc 的下一章 envelope 激活 | ExperienceContextProvider 与后续计划物化 | 范围、生效章与来源 Canon 身份一致 |
| world pressure | StateContextProvider、PersonalityContextProvider、Writer 世界压力段 | 当前按 `before_chapter` 取最新，尚不能证明任意旧结果可用 |
| feedback maintenance row | 聚合读侧与 order controls/barrier | 自动反馈隔离不代表可删除维护行或绕过义务/checkpoint 顺序 |

四步 barrier 保留。初次离线评估时没有启动 A4。随后固定 `c62f6d0` 的[隔离 smoke](2026-09-09-stage1-smoke.md)取得首章实际 3 场景、8 次逻辑 Writer 调用、466,158 ms，以及场景/stitch 时间矛盾未被评审拦住的证据，已具备调查调用成本与一致性的理由。不过这只是单路单章数据，没有同起点、同上下文、同预算的 single/scene 对照；无法推出改用 single 就能修复矛盾或达到 15% 质量不退步的收益。

后续 `1614560` smoke 再次出现分场与 stitch 保留编号矛盾。对现有 single 加上相同三次结构化抽取的候选，先执行了离线合同对照：相同 BODY、相同时间字段与地图路线，保留两个场景位置时 `MapMovementReviewer` 报出 `map_travel_time_exceeds_chapter_time`，single 产物没有场景记录，检查直接返回 pass。实际执行原 Writer/抽取/地图 owner，4 次夹具 adapter 调用、0 次真实模型调用。固定正文重复填充只满足长度解析，不作为文学质量样本。

**该直接替换候选拒绝，未进入真实模型比较。** 保留 reviewer 的注册并不能保证其输入和覆盖不变；这也不代表全部 Canon 层都会接纳坏正文。现存 scene→single 异常回退已经有同一限制，不能把扩大使用包装成等价。single 与 scene 提示的历史条数及约束重复也不同，后续对照须控制这些因素。当前没有可报告的 token、延迟或盲评收益，没有永久第二套 Writer。原始合同探针在私有审查目录 `review/task19-a4-map-parity.json`，独立基线报告在 `review/task19-writer-baseline.md`。

本地审查附件保存在忽略目录 `.superpowers/sdd/2026-09-09-forwin-three-stage/review/stage2-diagnostics/`：`replay.py`、`test_tape.py`、`post-a0-results.json`、`post-a0-input-lock.json`。原 `tape.json` 的 SHA256 为 `d68e62a544630df9a0754106c5558d8bd2678e9f64c56c9d64b33c8ef9696531`；原 `labels.json` 为 `8e1db536d7d755cb563a366d39775b9eb8ad1eddeb0061d763b513f7e3b2414b`，均未变化。A0 前报告与结果保留，未重写；重放禁止刷新响应并校验标签、原结果及所有输入键。

本报告关闭上述两个有界离线候选；Stage 2 全量回归及其他阶段验收状态由实施计划分别记录。
