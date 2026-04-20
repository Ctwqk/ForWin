# ForWin V2.8.1 完成情况（已并入 V2.9.2）

更新时间：2026-04-19

评估对象：`V2_8_1.md` 历史基线。  
当前统一规格：`V2_9_2.md`。  
评估基线：当前主干代码；当前主干已经把 `V2.8.1` 主链、`V2.9` subworld 控制，以及 `V2.9.2` Book Genesis 根层前置化整合为当前统一规格。

状态定义：

- `完成`：在 `V2_8_1.md` 的设计范围内已形成主路径闭环，并有代码与回归验证支撑。
- `基本完成`：设计主链已闭环，但仍存在质量增强项或运营级增强项。
- `非阻断增强`：与质量、运营、长期自治有关，但不阻止宣告 `V2.8.1` 完成。

---

## 1. 总体判断

当前可以正式宣告：

> `V2.8.1` 已在当前主干完成收口。  
> 当前主干的统一规格已经前推为 `V2.9.2`；`V2.8.1` 作为历史基线保留，不再单独承担“当前完整规格”的职责。

---

## 2. 主链状态

| 模块 | 状态 | 当前判断 |
| --- | --- | --- |
| V2.3 生产主链 | 完成 | Arc -> Band -> Chapter -> Scene、scene fallback、structured extraction、review / repair / canon 主闭环已成型。 |
| V2.6 反馈校准层 | 完成 | signal -> aggregate -> trend -> hint 主链、reader estimate、action mapper、audience hint 注入已闭环。 |
| V2.7 体验审查层 | 完成 | overlay、WNER、lint integration、experience review、repair instruction 已闭环。 |
| V2.8 治理层 | 完成 | strict progression、checkpoint、constraint、decision event、causal replay、governance insights、restart safety 已闭环。 |

---

## 3. 与旧完成文档相比的收口修正

以下项目此前被写成“部分完成”或“仍需补完”，当前主干已不应继续挂为缺口：

### 3.1 V2.3 scene-era contract

当前已具备：

- `scene continuation`
- `lore_candidates`
- `timeline_hints`
- `writer_notes`
- scene-aware prompt / split pipeline / extraction / fallback

结论：不再属于 `V2.8.1` 未完成项。

### 3.2 V2.7 evidence loop

当前已具备：

- `confirmed_signals` 进入 reviewer context
- WNER evidence anchoring
- audience-only evidence 不能直接构成 hard fail
- review notes / repair instruction / evidence refs 主路径闭环

结论：不再属于 `V2.8.1` 阻断项。

### 3.3 V2.6 calibration quality

当前已具备：

- 平台指标优先的 reader estimate
- aggregate / trend / hint 主链
- action effectiveness 统计接入 governance insights

结论：在 `V2.8.1` 设计范围内已完成；后续只剩质量增强，不再视为版本阻断。

### 3.4 V2.8 director governance

当前已具备：

- issue group
- director imbalance rules
- arc causal replay
- governance insights
- active generation task 检查与 restart safety 接口

结论：`V2.8.1` 所要求的治理闭环已经成立。

---

## 4. 版本边界

### 4.1 属于 V2.8.1 的收口范围

- Writer 主链、review / repair / canon、scene-era contract
- feedback calibration 主链
- WNER / overlay / lint / repair
- strict governance / checkpoint / constraints / decision timeline / causal replay
- task-center / runtime progress / restart safety

### 4.2 已前推并入 V2.9.2 的能力

当前主干已把以下能力并入 `V2.9.2` 统一规格：

- `sub_worlds`
- `sub_world_roster_items`
- `SubWorldManager`
- chapter-level allowed entities / entry targets / admission guard
- `BookGenesisRevision`
- `BookGenesisPack`
- `PromptTrace`
- `start-writing` handoff
- per-arc persisted sizing inheritance

其中 `subworld` 能力原本来自 `V2_9.md`；Genesis 根层能力则来自 `V2.9.2` 增量。现在它们已经被合并进 `V2_9_2.md`，不再只是“加法基线”，而是当前主干统一规格的一部分。  
它们不影响 `V2.8.1` 已完成这一判断，但会影响“当前主干应该看哪份规格文档”。

---

## 5. 非阻断增强项

以下仍可继续增强，但不影响宣告 `V2.8.1` 完成：

1. 完整运营级 trope library。
2. 真实读者规模和 action effectiveness 的长期校准。
3. WNER 证据判断与 heuristic 的持续调优。
4. override 统计驱动的自动规则校准。
5. 平台链路、world simulator、blackbox 长期自治的进一步强化。

这些项目要么属于质量增强，要么已超出 `V2_8_1.md` 第 13 节的承诺边界。

---

## 6. 验证状态

最近一次本地全量回归：

```bash
PYTHONPATH=. pytest -q
```

结果：

```text
208 passed, 8 subtests passed
```

当前收口相关关键能力已覆盖：

- writer split pipeline
- audience feedback alignment
- governance review / checkpoint / decision API
- generation control / runtime progress
- generation task persistence / restart safety
- subworld control

---

## 7. 最终结论

结论保持明确：

- `V2.8.1` 已完成正式收口。
- `V2_8_1.md` 继续作为历史基线保留。
- 当前主干的完整统一规格应以 `V2_9_2.md` 为准。
- 后续若继续扩展 Genesis、subworld、trope 库、自动校准，应按 `V2.9.2+` 或后续增量版本处理，而不是继续把它们挂成 `V2.8.1` 未完成项。
