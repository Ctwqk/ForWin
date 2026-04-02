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
- 如果要清掉这份测试 profile，可以运行：[`scripts/reset_linux_extension_browser.sh`](/home/taiwei/ForWin/scripts/reset_linux_extension_browser.sh)

注意：

- 这份 Linux profile 只用于服务器侧联调，不会替代你在 macOS 浏览器里的真实用户会话
- 如果你平时在 Mac 上使用 ForWin，Mac 浏览器里的扩展仍然需要单独安装
