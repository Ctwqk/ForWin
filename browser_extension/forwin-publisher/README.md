# ForWin Publisher Bridge

开发者模式加载目录：

`browser_extension/forwin-publisher`

首次使用：

1. 在 Chrome / Chromium 的扩展管理页打开开发者模式。
2. 选择“加载已解压的扩展程序”，指向本目录。
3. 打开扩展设置页，填写：
   - `ForWin Backend URL`
   - `Extension API Key`
4. 回到 ForWin 的 `/publishers` 页面，确认扩展已被检测到。

当前第一版能力：

- 起点扫码登录
- 番茄扫码登录
- 保存草稿 / 直接发布
- 扩展心跳回写

当前明确不做：

- 服务端浏览器会话
- 服务器端二维码中转
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
