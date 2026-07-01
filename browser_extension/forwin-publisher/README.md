# ForWin Publisher Bridge

源码目录：

`browser_extension/forwin-publisher`

构建双目标产物：

- `npm run build`
- `npm run build:chromium`
- `npm run build:firefox`

构建输出目录：

- `browser_extension/dist/forwin-publisher-chromium`
- `browser_extension/dist/forwin-publisher-firefox`

首次使用：

1. 先在扩展目录执行目标构建，例如 `npm run build:chromium` 或 `npm run build:firefox`。
2. 打开对应浏览器的扩展调试页。
3. 选择要加载的构建产物目录：
   - Chromium：`browser_extension/dist/forwin-publisher-chromium`
   - Firefox：`browser_extension/dist/forwin-publisher-firefox/manifest.json`
4. 打开扩展设置页，填写：
   - `ForWin Backend URL`
   - `Extension API Key`
   - 保持“允许把平台登录二维码发送给后端通知通道”关闭，除非正在执行一次明确的临时转发窗口
5. 回到 ForWin 的 `/publishers` 页面，确认扩展已被检测到。

浏览器说明：

- Chromium 目标保留当前 `debugger` 路径，适合上传、可信输入、可信点击和服务端自动化。
- Firefox 目标共享登录、cookie 同步、心跳、内容桥接和普通 cookie 恢复能力。
- Firefox 不支持 Chromium 的 `debugger` API；触发这类动作时，扩展会返回显式能力错误，而不是静默降级。

Firefox 临时加载：

1. 运行 `npm run build:firefox`
2. 打开 `about:debugging`
3. 进入 `This Firefox`
4. 选择 `Load Temporary Add-on`
5. 指向 `browser_extension/dist/forwin-publisher-firefox/manifest.json`

当前第一版能力：

- 起点扫码登录
- 番茄扫码登录
- Routine production login continuity uses backend-synced browser sessions.
  共享生产 Swarm 中，`forwin-publisher-browser-swarm` 的持久化 profile 是番茄和
  起点共用的 production publisher browser profile。浏览器启动时会恢复后端同步的
  session，打开 `/publishers`，并通过 extension heartbeat 把页面证据回写给后端。
  使用以下命令验证登录状态：

  ```bash
  python scripts/check_production_publisher_baseline.py \
    --api-base http://10.0.0.126:8899 \
    --mcp-health-url http://10.0.0.126:8896/health \
    --docker-context swarm-manager-150 \
    --colima-profile swarmbridged
  ```

  如果检查返回 `publisher_login_required`，只在 production publisher browser
  profile 中完成平台登录，然后重跑同一条 baseline 命令。共享生产的 routine
  登录恢复路径不向 Discord 发送二维码或登录确认。
  `FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK=true`、
  `FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL` 和
  `FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE` 会被后端运行时忽略。扩展设置里的
  二维码通知开关默认关闭；即使旧 profile 里遗留
  `loginQrNotificationsEnabled=true`，没有隐藏的
  `loginQrNotificationsAllowed=true` 和未来的
  `loginQrNotificationsAllowedUntilMs` 临时时间窗时，扩展也不会截图或 POST
  `/api/publishers/extension/login-qr`。扩展心跳检测到登录页时只回写
  `login-required`。
- 保存草稿 / 直接发布
- 扩展心跳回写

当前明确不做：

- 服务端浏览器会话
- 短信验证码 / 滑块自动化

Linux 侧联调：

- 仓库内置了一个专用测试浏览器脚本：[`scripts/launch_linux_extension_browser.sh`](/home/taiwei/ForWin/scripts/launch_linux_extension_browser.sh)
- 它会用独立的 Chrome profile 加载当前扩展目录，并保留扩展设置、cookie 和登录态，方便反复做服务器侧 smoke test
- 默认打开 `http://127.0.0.1:8899/publishers`
- 启动脚本默认会检查 profile 是否已经写入当前 `FORWIN_BACKEND_URL` 和 `FORWIN_PUBLISHER_EXTENSION_API_KEY`；未写入时会用可用 display 自动初始化
- 启动脚本默认使用 `FORWIN_EXTENSION_DISPLAY_MODE=auto`：优先尝试 `FORWIN_EXTENSION_DISPLAY`（默认 `:100`，避免占用本机 vnc-manager 的 `:99`），再复用非 `:99` 的当前 `DISPLAY`，最后才退回 `xvfb-run`
- 如果要清掉这份测试 profile，可以运行：[`scripts/reset_linux_extension_browser.sh`](/home/taiwei/ForWin/scripts/reset_linux_extension_browser.sh)

WSL 侧直接启动：

- 先安装运行依赖：`sudo apt-get install -y xvfb chromium`
- 安装项目依赖：`python3 -m pip install -e .`
- 启动扩展浏览器：`scripts/launch_linux_extension_browser.sh`
- 如果 WSL 上没有 vnc-manager，脚本会通过 `xvfb-run` 现起一个 Xvfb，默认从 `:100` 开始找空位；如果有外部 VNC/X display，可以设置 `FORWIN_EXTENSION_DISPLAY=:100` 并使用 `FORWIN_EXTENSION_DISPLAY_MODE=external`

Docker Compose 侧联调：

- 后端照常启动：`docker compose up -d`
- 如果要把 Linux 扩展浏览器也交给 compose 管理，启动 profile：`docker compose --profile publisher-browser up -d`
- 扩展浏览器会使用 `/app/data/chrome_profiles/forwin-extension` 作为持久 profile，并通过 `http://forwin:8899` 访问后端
- 如需覆盖 compose 内部后端地址，设置 `FORWIN_PUBLISHER_BROWSER_BACKEND_URL`
- 如需调试浏览器，可通过 `FORWIN_EXTENSION_DEBUG_BIND` 调整远程调试端口绑定；默认只绑定到宿主机 `127.0.0.1:9222`

注意：

- 这份 Linux profile 只用于服务器侧联调，不会替代你在 macOS 浏览器里的真实用户会话
- 如果你平时在 Mac 上使用 ForWin，Mac 浏览器里的扩展仍然需要单独安装
