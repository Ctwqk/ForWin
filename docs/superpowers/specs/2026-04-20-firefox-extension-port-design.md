# ForWin Publisher Firefox Port Design

## Goal

在保留现有 Chromium 扩展能力和代码结构的前提下，为 `browser_extension/forwin-publisher` 增加一个 Firefox 目标产物。两端共享同一套业务逻辑、同一版本号和同一测试入口，但允许在 manifest 和浏览器能力上做显式分叉。

## Scope

本次设计覆盖：

- 现有扩展的双目标构建产物：`chromium` 与 `firefox`
- Firefox 可加载、可配置、可登录、可同步 cookie、可回写心跳
- 将浏览器差异收敛到 manifest 和 runtime capability 层
- 保持现有 Chromium 路径不回退

本次设计不承诺：

- Firefox 实现 Chromium `debugger` API 等价能力
- 使用同一个打包文件直接安装到两个浏览器
- 重写现有发布流程或平台自动化逻辑

## Current State

当前扩展目录为 `browser_extension/forwin-publisher`，主要特征如下：

- `manifest.json` 是 Chromium 风格的 Manifest V3，仅声明 `background.service_worker`
- 后台脚本 `background.js` 已通过 `lib/extension-runtime.js` 同时兼容 `browser` 与 `chrome`
- 业务能力中存在一组 Chromium 专属能力：`chrome.debugger` 驱动的可信点击、可信输入、cookie 注入兜底
- 现有测试覆盖集中在控制器和纯逻辑模块，没有覆盖 manifest 生成或浏览器能力矩阵
- Linux 联调脚本和 profile 预置脚本当前完全围绕 Chromium

## Constraints

### Firefox 平台约束

- Firefox 不实现 Chrome 的 `debugger` API，因此不能直接复用现有可信输入/点击路径
- Firefox 后台不能依赖 Chromium-only 的 `background.service_worker` 作为唯一入口
- Firefox 目标需要 `browser_specific_settings.gecko.id`，便于临时加载之外的稳定安装和更新

### 项目约束

- 不能复制一份独立的 Firefox 扩展目录，否则后续功能会漂移
- 不能让 Chromium 目标为了迁就 Firefox 而失去现有 `debugger` 兜底能力
- 需要让调用方在运行时知道当前浏览器支持哪些能力，而不是在业务分支里硬编码 UA 判断

## Recommended Architecture

### 1. 单代码库，双目标产物

保留 `browser_extension/forwin-publisher` 作为源码目录，不再把其中的 `manifest.json` 视为唯一发布产物，而是引入“源 manifest + 构建输出”的模式：

- 源码目录继续保存共享脚本、样式、页面、测试
- 新增浏览器目标构建脚本，产出：
  - `browser_extension/dist/forwin-publisher-chromium`
  - `browser_extension/dist/forwin-publisher-firefox`
- 两个目标共享相同 JS/HTML/CSS 文件，只在 manifest 和少量元数据上分叉

这样可以保证版本一致，同时把浏览器差异限制在构建层。

### 2. Runtime capability 抽象

在 `lib/extension-runtime.js` 中补一层能力描述，而不是只暴露 `extensionApi`：

- `browserTarget`: `chromium` 或 `firefox`
- `supportsDebugger`: 当前浏览器是否支持扩展调试协议
- `supportsBackgroundServiceWorker`: 当前目标是否走 service worker

后台逻辑继续通过共享控制器运行，但所有 `debugger` 相关路径统一先看 capability：

- Chromium：维持现状
- Firefox：对需要 `debugger` 的路径返回明确的“不支持”结果，或仅走无调试协议的普通 API fallback

这样做可以避免未来在 `background.js` 里继续扩散 `if (firefox)` 分支。

### 3. 目标化 manifest

构建脚本应生成两个 manifest：

- Chromium manifest
  - 保留 `manifest_version: 3`
  - 保留 `background.service_worker`
  - 保留 `debugger` 权限
- Firefox manifest
  - 保留 `manifest_version: 3`
  - 使用 `background.scripts` 加 `type: "module"`
  - 添加 `browser_specific_settings.gecko.id`
  - 去掉 Firefox 不支持的 `debugger` 权限

Firefox manifest 的目标不是“伪装成 Chromium”，而是对 Firefox 声明真实可用能力。

### 4. 平台能力行为定义

为避免 Firefox 版行为模糊，能力边界需要固定：

- 必须支持
  - 选项页配置
  - 内容脚本桥接
  - 登录弹窗
  - cookies 读取与同步
  - 后端心跳
  - 读取后端已保存会话并恢复 cookies
- 条件支持
  - 上传流程中所有不依赖 `debugger` 的步骤
  - 使用 `cookies.set` 的普通 cookie 恢复路径
- 显式降级
  - 可信点击
  - 可信文本注入
  - 任何直接依赖 `chrome.debugger` 的操作

这样 Firefox 版可以稳定承担登录与会话同步职责，而 Chromium 版继续承担最强自动化职责。

## Data Flow

### 登录与会话同步

Firefox 与 Chromium 共享同一条主链路：

1. 用户在扩展选项页填写后端地址与 API Key
2. 后台脚本启动控制器并执行心跳
3. 用户从 ForWin 页面触发平台登录
4. 扩展打开平台登录页
5. 登录成功后后台读取 cookies
6. 扩展将 cookies 和浏览器信息同步到 ForWin 后端
7. 服务器侧 Chromium worker 继续使用这份会话

这条链路不依赖 `debugger`，因此应该作为 Firefox 端的首要保证路径。

### 上传与可信输入

上传链路保持共享控制器，但在触发需要 `debugger` 的分支时：

- Chromium 继续走现有可信输入逻辑
- Firefox 直接返回 capability-limited 结果，让上游知道当前浏览器不支持该动作

这比静默失败更安全，也更容易在后端或页面上做后续提示。

## Error Handling

需要新增两类显式错误：

- `unsupported-browser-capability`
  - 用于当前浏览器不支持 `debugger`、service worker 等能力时
- `unsupported-browser-target`
  - 用于构建产物和运行环境不匹配，或 manifest 目标错误时

所有 capability 错误都应包含：

- 当前 `browserTarget`
- 缺失的 capability 名称
- 建议动作，例如“请在 Chromium 版扩展中执行该上传任务”

## Testing Strategy

### Automated tests

新增测试应覆盖：

- runtime capability 检测
- Chromium/Firefox manifest 生成结果
- Firefox 目标下 `supportsDebugger === false` 时的降级行为
- 现有控制器测试在 Chromium 默认行为下不回归

### Manual verification

至少验证以下场景：

- Chromium 目标构建后仍能正常加载
- Firefox 目标可在 `about:debugging` 临时加载
- Firefox 选项页可保存设置
- Firefox 能完成起点/番茄登录并把 cookies 同步回后端
- Firefox 上触发 `debugger` 依赖操作时给出明确错误，而不是静默失效

## File-Level Design

预期会涉及这些文件：

- `browser_extension/forwin-publisher/background.js`
  - 改为通过 capability 决定是否开放 `debugger` 路径
- `browser_extension/forwin-publisher/lib/extension-runtime.js`
  - 新增浏览器目标与 capability 探测
- `browser_extension/forwin-publisher/manifest.json`
  - 转为源 manifest 或模板输入，而非唯一发布 manifest
- `browser_extension/forwin-publisher/package.json`
  - 增加构建脚本和 Firefox 目标测试入口
- `browser_extension/forwin-publisher/tests/*.test.js`
  - 补 capability 与 manifest 构建测试
- `browser_extension/forwin-publisher/README.md`
  - 增加 Firefox 加载与限制说明
- 新增构建脚本文件
  - 负责生成两套目标目录和目标 manifest

## Implementation Notes

推荐使用“构建时生成 manifest”的方式，而不是运行时在一个 manifest 中尝试兼容所有浏览器。原因是：

- Chromium 与 Firefox 的后台字段支持不一致
- `debugger` 权限不能安全地原样塞给 Firefox
- Firefox 的 `browser_specific_settings` 不应污染 Chromium 目标

构建脚本应尽量简单，优先使用 Node 原生文件 API，而不是引入新的打包器。

## Decision Summary

最终采用：

- 单代码库
- 双目标产物
- 共享业务逻辑
- manifest 构建分叉
- runtime capability 显式建模
- Firefox 对 `debugger` 相关能力做显式降级

这个方案能保证你的 Firefox 端尽快可用，同时不破坏服务器和 Chromium 端已有自动化能力。
