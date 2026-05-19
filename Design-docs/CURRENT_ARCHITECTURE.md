# ForWin Current Architecture

更新时间：2026-05-06

状态：active-current。本文档是当前架构入口；旧 V2/V3/V4 side-by-side 计划只作为历史背景或兼容说明读取。

## 当前权威口径

ForWin 当前工程基线是：

```text
Genesis / Writer / Review 主链
+ Skill Runtime
+ Observability
+ Final BookState Runtime
+ Scheme C BookMap
= 当前工程基线
```

唯一 canon source 是 `BookState DB Canon`。章节生产仍由 `Arc -> Band -> Chapter -> Scene` 驱动；入 canon 的最终路径是 `BookStateGraphDeltaExtractor -> BookStateReviewGate -> BookStateCompiler -> GraphDelta ledger -> Snapshot`。

## Runtime 边界

- 书本根真值：`Genesis`。
- 世界状态 canon：`BookState + GraphDelta + Snapshot`。
- 地图 canon：`BookMap / Scheme C`，语义为 `SubWorld -> Region -> MapNode -> MapEdge`。
- 上下文来源：`BookState + BookMap + Genesis + approved projections`。
- review 主链：`reviewer.HistoricalReviewHub` 聚合章节文本、体验、治理、地图、人格和 lint；`BookStateReviewGate` 是 GraphDelta 入 canon 前的 deterministic guardrail。
- skill runtime：仅作为 prompt / workflow instruction layer，参与 PromptTrace，不写 canon，不绕过 DecisionEvent 或 BookState gate。

## Quality Profile

ForWin supports `quality_profile=standard|pulp|premium`.

`standard` is the default and preserves the existing long-form quality path.
`pulp` derives a low-cost runtime profile from config: single-call writer mode, deterministic review, fatal-only canon admission, hard floor checks, world-only BookState extraction, context recency truncation, and low-cost trope selection.
`premium` is reserved for future defaults and currently behaves like standard unless explicit config fields override it.

## Canon Commit Path

新章节 accepted 后，canon success 只以 BookState direct path 为准：

```text
WriterOutput / chapter body
-> BookStateGraphDeltaExtractor
-> BookStateReviewGate
-> BookStateCompiler
-> projection refresh
```

`world_v4` rows 只在 `FORWIN_WORLD_V4_COMPAT_WRITE=true` 时作为 best-effort compatibility projection 写入。该投影失败不得回滚已提交的 BookState canon；关闭该开关时，新项目仍可完成 BookState canon commit。

## 兼容层

- `world_model`：legacy wiki/export/projection/read path；不作为新 canon 语义来源。
- `world_model_v4`：world_v4 compatibility projection / migration source / debug-export bridge；不继续新增最终 canon 能力。
- `reviewer_v4`：world_v4 extraction compatibility gate；不是 `reviewer` 的新版替代品。
- legacy `entities / entity_states / relation_edges / CanonEvent`：兼容投影、迁移输入或审计摘要。
- legacy provisional：历史预演、审计和 compatibility preview，不默认阻断正式写作。

## 投影层

`Obsidian Vault`、Karpathy-style `LLM KB`、legacy wiki/export 和 World Studio 视图都必须可从 BookState 或兼容投影重建。它们不是 canon writer。

## 地图红线

`SubWorld` 只表示大陆、星球、位面、异世界、星区等大尺度地图容器。城市、宗门外门、客栈、遗迹入口、炼丹塔等局部舞台必须进入 `Region / MapNode / site_state`，不得作为新的 `SubWorld` 语义写回设计或代码。
