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
