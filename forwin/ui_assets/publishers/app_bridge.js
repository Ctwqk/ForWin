    const EXTENSION_BRIDGE_CHANNEL = 'forwin-publisher-extension';
    const BACKEND_EXTENSION_KEY_READY = @@BACKEND_EXTENSION_KEY_READY@@;
    const EXTENSION_INSTALL_PATH = @@EXTENSION_INSTALL_PATH_JSON@@;
    const pendingBridgeRequests = new Map();
    let uploadPollTimer = null;
    let uploadJobsPollTimer = null;
    let selectedPlatformId = '';

    function bridgeId() {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
      return `forwin-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

{PAGE_DOM_HELPERS_JS}

    function normalizeOrigin(value) {
      try {
        return new URL(value).origin;
      } catch (_error) {
        return '';
      }
    }

    function showManualExtensionSetup(extraMessage = '') {
      const currentOrigin = window.location.origin;
      const lines = [
        '首次安装请手动完成这几步：',
        '1. Chrome/Edge 点击“下载扩展包（Chrome/Edge）”；Firefox 点击“下载 Firefox 扩展包”。',
        '2. Chrome/Edge 解压后得到 forwin-publisher；Firefox 解压后得到 forwin-publisher-firefox。',
        '3. Chrome/Edge 打开 chrome://extensions 或 edge://extensions，开启开发者模式，加载解压后的 forwin-publisher 文件夹。',
        '4. Firefox 打开 about:debugging#/runtime/this-firefox，点击“临时载入附加组件”，选择 forwin-publisher-firefox/manifest.json。',
        '5. 打开扩展的设置/选项页面。',
        `6. 在扩展里把 ForWin Backend URL 填成：${currentOrigin}`,
        '7. Extension API Key 填你服务器 .env 里的 FORWIN_PUBLISHER_EXTENSION_API_KEY。',
        '8. 保存后刷新当前 /publishers 页面。',
      ];
      if (extraMessage) {
        lines.push('', `补充信息：${extraMessage}`);
      }
      window.alert(lines.join('\\n'));
    }

    function setPlatformStatus(platform, text, kind = 'warn') {
      const el = document.getElementById(`status_${platform}`);
      if (!el) return;
      el.textContent = text;
      el.className = `status ${kind}`;
    }

    function renderExtensionSummary(details = null, error = '') {
      const el = document.getElementById('extension_summary');
      if (!BACKEND_EXTENSION_KEY_READY) {
        el.textContent = '后端还没有配置扩展 API Key。请先设置 FORWIN_PUBLISHER_EXTENSION_API_KEY，再让扩展连接这个实例。';
        el.className = 'status warn';
        return;
      }
      if (!details) {
        el.textContent = `未检测到浏览器扩展。请确认你已经用开发者模式加载：\\n${EXTENSION_INSTALL_PATH}\\n并且在扩展设置里把当前 ForWin 地址加入桥接。${error ? `\\n${error}` : ''}`;
        el.className = 'status warn';
        return;
      }
      const configuredOrigin = normalizeOrigin(details.backendBaseUrl);
      const currentOrigin = window.location.origin;
      const sameBackend = configuredOrigin === currentOrigin;
      el.textContent = [
        '浏览器扩展已连接',
        `客户端：${details.browserName || 'unknown'} / ${details.extensionVersion || 'dev'}`,
        `扩展配置的后端：${details.backendBaseUrl || '未配置'}`,
        `当前页面后端：${currentOrigin}`,
        sameBackend ? '后端地址匹配，可以直接登录和上传。' : '扩展里的后端地址和当前页面不一致，请先去扩展设置里改成当前地址。',
      ].join('\\n');
      el.className = `status ${sameBackend ? 'ok' : 'warn'}`;
    }

    function bridgeRequest(action, payload = {}, timeoutMs = 1800) {
      return new Promise((resolve, reject) => {
        const correlationId = bridgeId();
        const timer = window.setTimeout(() => {
          pendingBridgeRequests.delete(correlationId);
          reject(new Error('浏览器扩展未响应。'));
        }, timeoutMs);
        pendingBridgeRequests.set(correlationId, { resolve, reject, timer });
        window.postMessage(
          {
            channel: EXTENSION_BRIDGE_CHANNEL,
            direction: 'page-to-extension',
            kind: 'request',
            correlationId,
            action,
            payload,
          },
          window.location.origin,
        );
      });
    }

    async function pingExtension() {
      try {
        const payload = await bridgeRequest('ping');
        renderExtensionSummary(payload);
        return payload;
      } catch (error) {
        renderExtensionSummary(null, error.message || String(error));
        return null;
      }
    }

    async function loadPlatforms() {
      const res = await fetch('/api/publishers/platforms');
      const data = await res.json();
      const grid = document.getElementById('platforms');
      const select = document.getElementById('platform');
      const nextSelectedPlatformId = selectedPlatformId || select.value || '';
      clearNode(grid);
      clearNode(select);
      data.forEach((item) => {
        const loggedIn = item.connected && !['login-required', 'platform-login-required'].includes(String(item.last_error || '').trim());
        const option = document.createElement('option');
        option.value = item.platform_id;
        option.textContent = item.display_name;
        if (item.platform_id === nextSelectedPlatformId) {
          option.selected = true;
        }
        select.appendChild(option);

        const heartbeat = item.last_heartbeat_at ? `最近心跳：${item.last_heartbeat_at}` : '还没有收到扩展心跳';
        const online = item.extension_online ? '扩展在线' : '扩展离线';
        const card = document.createElement('div');
        card.className = 'card';
        card.appendChild(createNode('h2', item.display_name));
        if (item.extension_client_id) {
          card.appendChild(createNode('p', `当前执行端 Client ID：${item.extension_client_id}`, 'muted'));
        }
        const loginText = document.createElement('p');
        loginText.appendChild(document.createTextNode('登录入口：'));
        const loginLink = document.createElement('a');
        loginLink.href = item.login_url;
        loginLink.target = '_blank';
        loginLink.rel = 'noreferrer';
        loginLink.textContent = item.login_url;
        loginText.appendChild(loginLink);
        card.appendChild(loginText);
        card.appendChild(createNode('p', `支持登录：${item.supported_login_methods.join(' / ') || 'scan'}`, 'muted'));
        card.appendChild(createNode('p', `支持动作：${item.supported_actions.join(' / ')}`, 'muted'));
        const actions = createNode('div', '', 'actions');
        actions.appendChild(createButton(loggedIn ? '重新连接' : '连接平台', () => connectPlatform(item.platform_id)));
        actions.appendChild(createButton('仅打开官网', () => openOfficialSite(item.login_url, item.platform_id), 'secondary'));
        card.appendChild(actions);
        const status = createNode(
          'div',
          `${loggedIn ? '已连接' : '未连接'} | ${online}\\n${heartbeat}`,
          `status ${loggedIn ? 'ok' : 'warn'}`,
        );
        status.id = `status_${item.platform_id}`;
        card.appendChild(status);
        if (item.last_error) {
          card.appendChild(createNode('p', `最近错误：${item.last_error}`, 'status warn'));
        }
        grid.appendChild(card);
      });
      if (!select.value && data.length) {
        select.value = data[0].platform_id;
      }
      selectedPlatformId = select.value || '';
    }

    function openOfficialSite(url, platform) {
      window.open(url, '_blank', 'noopener,noreferrer');
      setPlatformStatus(platform, '已在浏览器里打开平台官网，但这一步不会自动通知后端登录成功。请优先用“连接平台”让扩展接管整个登录流程。', 'warn');
    }

    async function connectPlatform(platform) {
      if (!BACKEND_EXTENSION_KEY_READY) {
        setPlatformStatus(platform, '后端未配置扩展 API Key，暂时无法接收扩展心跳和任务回写。', 'warn');
        return;
      }
      setPlatformStatus(platform, '正在请求扩展打开登录弹窗...', 'warn');
      try {
        const response = await bridgeRequest('open-login', { platform }, 2500);
        setPlatformStatus(platform, response.message || '登录弹窗已打开，请在弹窗里完成扫码。', 'warn');
      } catch (error) {
        setPlatformStatus(platform, error.message || String(error), 'warn');
      }
    }
