# Genesis 完整地图输出修复

## 发现与范围

冻结候选 `b61a7184ad198f4cac5d481c8998da0baf5a18f1` 通过 3203 项全量回归、4 项跳过及两类完整角色镜像验证。其新隔离项目在 Map revision 6 停留于写前检查：Brief、World 已锁定，Map 未锁定，没有章节任务或 observer。

实际发给模型的 output schema 只描述并要求 `edges`，但提示词要求完整六字段地图。模型响应只有十三条路线；归一化补入默认的一张总图、两个区域和三个节点。十三条路线原文没有改变，但二十六个端点引用全部悬空。原 importer 在第一条路线明确拒绝；本次没有进入正文生成，也不能据此声称正文或 Canon 已被污染。

证据位于运行目录 `forwin-stage1-smoke-b61a7184ad19-c0a2f1` 的 `20-map-read.json`、`21-map-events.json` 和 `map-completeness-diagnosis/`。原始 artifact 来自 canonical 事件给出的路径，并与事件 SHA 核验：

- request：`f268c4fc777201ae8dad8d63d5f0af17aaaf0f3470c16547385d65ed999bf273`
- response：`f1a613056c1f1013b6f613c4c6c4060e529112bfbef8eb6ea96257ae18e3c62d`

此样本保留原内容，不手工补地图或恢复为替换源码的验收样本。

## 修复

`forwin/map/genesis_atlas.py` 描述既有 MapAtlas 六字段及 SubWorld、Region、Node 结构，复用唯一的路线 parser。新完整生成要求全部字段及显式地点 ID；完整修订也验证全部字段，但允许既有路线输入形式。解析后的内容仍保留原始字典，不把模型响应替换为 schema 默认对象。

引用校验在归一化之前执行，并检查归一化结果；Map 锁定、合并后的 patch/定向修订和导入使用相同约束。它拒绝重复身份、悬空或歧义端点、错误父级、跨 SubWorld 区域归属及错误容器。两级 Region 的父级索引与数组顺序无关；ID 优先于名称，重名或 ID/名称碰撞时不把有效 ID 转成歧义名称。

初始 World 和手工输入可以继续补齐省略的稳定 ID；显式空集合保持为空，既有无 authored edges 的程序化行为保留。已有节点父级别名在一个共享入口解释，导入器的重复父级裁决删除。未知或错误类型不能借别名被覆盖，校验不修改调用者原字典。历史 revision 的读取不增加硬门。

完整模型响应缺项或结构错误沿现有合同异常明确拒绝，不回退为另一张地图。显式非对象 `map_atlas` 在 WorldRoot 转换前被拒绝。没有增加 LLM 质量门、连通性策略或地图规模要求，也没有修改运行中的样本和已部署地图。

## 验证与剩余工作

首次新增回归为 18 failed / 1 passed，证明缺项、结构错误及五个写入口会错误接受。随后独立审查与失败测试覆盖父子顺序、重名/ID 碰撞、非对象容器、旧节点父级别名、原输入不可变，以及原有手工 PATCH 自动补 ID 的兼容行为。旧路线测试的一处模型 fixture 补齐了新增完整输出要求的 `topology_rules`，未放宽其路线拒绝断言。

同一真实原响应的离线重放现在明确报告缺失五个地图字段；原归一化地图也被共享引用校验拒绝。原 artifact SHA 不变，无数据库或模型调用。这证明输入边界修复，不证明新的真实模型输出或正文质量。

最终相关回归 **235 passed**，独立有界复核 **167 passed**，计数重叠；已发现的阻断项消除。关键静态检查、编译和差异检查通过。冻结全量 QA 和精确角色镜像证据由执行计划及本地候选 evidence 继续记录。smoke20、同配置的全新独立 L100 和实际结局审读仍未完成；历史工程通过不能替代它们。没有 push、合并、部署或生产业务写入。

## Task 23：完整修订 schema 与模型失败回退

后续冻结 `45aa5309f9a275322868c83f87a97d53279af7b1` 完成 3372 项全量回归和双角色镜像验证。新项目 `51b08a3ee72945148465e980a9c16ba8` 的 Brief、World 锁定后，两次 Map 生成均因具体 Node 指向 Region 而被拒绝；区域真实存在，不能将其称为不存在的地点。原 revision 7 和完整 pack 未变。两份真实原响应按相同生产 validator 重放分别在 `edges[6]` 和 `edges[5]` 拒绝；端点校验正确。

写前地图修订仅补齐候选拓扑中已有的处置场为具体 Node，并使仓库 ID 对齐已锁 World 的引用。该候选的一张总图、两个区域、七个地点、六条路线通过现有完整校验。但正式 MCP 全量 refine 返回成功后，canonical readback 显示 revision 8 的 Map 为 edited，地点和路线均为空，未采用核对过的候选。未锁定此结果，也未启动章节生产。

保留证据证明两个独立实现边界：完整 refine 的输出 schema 含开放字典 `edges.items.additionalProperties=true`，被 Codex 严格结构化输出接口明确拒绝；普通模型回退接口随后因余额/额度不足失败。Genesis 原逻辑只对路线合同异常停止，对提供方错误、解析耗尽或缺凭据仍返回程序化 fallback，导致失败被记为修订成功。额度问题需要恢复账户，不能由代码虚构响应解决。

修复使用已有 JSON 模式请求完整 Map 修订，返回后仍执行完整六字段和旧路线/引用校验；新生成保留严格 canonical schema。删除不再使用的开放字典 schema 变体。Map 生成、完整和定向修订的缺模型或既有重试耗尽均明确失败，workspace 在保存前停止；非 Map 的回退行为保持。没有放宽路线规则、修改旧数据、增加重试或切换模型。

运行目录 `forwin-stage1-smoke-45aa5309f9a2-c94fd4` 保存原调用、canonical 读回、地图提案和 `map-diagnosis/index.json`；30 个原始文件逐一验证远端/本地 SHA。桥接会话的最终响应与模型标识另有记录，真实使用 `gpt-5.6-sol`。初次失败的 Map artifacts 当时未落盘，在后续 trace 保存时才被保留，不能声称其在失败当时就有完整持久 trace。`37-active-before-stop.json` 确认无生成任务，随后停止五个隔离容器，卷及证据保留。

新增失败合同正确 RED 为 **19 failed / 3 passed**；修复后 **80 项聚焦测试**、**315 项相关测试**通过，计数重叠。六个 PostgreSQL 用例在捕获失败后显式 commit，再确认正式 revision ID、原 JSON 与 revision 数量均不变。独立实施复审的 47 项纯测试、12 个 Map 失败组合、28 个非 Map 旧新对照、12 个 workspace 保存边界、旧路线等价及实际 Codex 转换均通过，无阻断发现。完整 Ruff 对原基线零新增诊断，关键静态与差异检查通过。新源码的冻结完整 QA、镜像仍需另记；此处不宣称新模型修订、smoke20、L100 或结局已通过。

冻结 `89aa84f` 的完整 QA 为 **3393 passed / 1 failed / 4 skipped / 6 subtests passed**。唯一失败是兼容性清单漏登记 `forwin/genesis/llm.py` 的完整修订入口；编译、关键静态、源码策略与差异检查通过，完整 Ruff 相对基线零新增诊断。将该入口、精确匹配文本、JSON 模式及保存前校验理由和失败回归登记到既有 `genesis.persisted_route_input` 项，保留原审计断言与运行代码。此修正需要新提交的完整 QA，不能将本轮失败记为通过。
