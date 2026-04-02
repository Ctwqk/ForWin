import {
  PLATFORM_ADAPTERS,
  buildHeartbeatState,
  getPlatformAdapter,
  getProbeUrl,
  shouldProbeLogin,
} from './platforms.js';

export class PublisherExtensionController {
  constructor(deps) {
    this.deps = deps;
    this.loginSessions = new Map();
    this.dispatchInFlight = null;
  }

  async bootstrap() {
    await this.deps.ensureClientId();
    await this.deps.ensureHeartbeatAlarm();
    await this.sendHeartbeat();
    await this.syncConnectedSessionsToBackend();
    await this.dispatchPendingUploadJobs();
  }

  async handleMessage(message, sender = {}) {
    const action = String(message?.action || '').trim();
    const payload = message?.payload || {};
    if (action === 'ping') {
      return this.ping();
    }
    if (action === 'open-options') {
      await this.deps.openOptionsPage();
      return { message: '扩展设置页已打开。' };
    }
    if (action === 'settings-updated') {
      await this.deps.refreshContentBridge();
      await this.sendHeartbeat();
      await this.syncConnectedSessionsToBackend();
      await this.dispatchPendingUploadJobs();
      return { message: '扩展设置已更新。' };
    }
    if (action === 'open-login') {
      return this.openLogin(String(payload.platform || '').trim(), sender?.tab?.id || 0);
    }
    if (action === 'execute-upload-job') {
      return this.executeUploadJob(String(payload.jobId || '').trim(), sender?.tab?.id || 0);
    }
    throw new Error(`Unsupported action: ${action}`);
  }

  async ping() {
    const settings = await this.deps.getSettings();
    const browserInfo = await this.deps.getBrowserInfo();
    return {
      detected: true,
      clientId: await this.deps.getClientId(),
      extensionVersion: this.deps.getExtensionVersion(),
      browserName: browserInfo.browserName,
      browserVersion: browserInfo.browserVersion,
      backendBaseUrl: settings.backendBaseUrl,
    };
  }

  async openLogin(platformId, originTabId) {
    const adapter = getPlatformAdapter(platformId);
    await this.closeExistingPlatformSession(platformId);
    await this.deps.setPlatformState(platformId, {
      connected: false,
      loginMethod: 'scan',
      lastError: '',
    });
    await this.sendHeartbeat();
    const popup = await this.deps.openLoginPopup(adapter.loginUrl);
    const session = {
      platformId,
      originTabId,
      popupTabId: popup.tabId,
      popupWindowId: popup.windowId,
      probeIndex: 0,
      lastUrl: adapter.loginUrl,
    };
    this.loginSessions.set(popup.tabId, session);
    await this.deps.notifyPage(originTabId, 'login-status', {
      platform: platformId,
      connected: false,
      message: `${adapter.displayName} 登录弹窗已打开，请在弹窗里完成扫码。`,
    });
    return { message: `${adapter.displayName} 登录弹窗已打开。` };
  }

  async executeUploadJob(jobId, originTabId) {
    if (!jobId) {
      throw new Error('缺少上传任务 ID。');
    }
    const job = await this.deps.backend.getUploadJob(jobId);
    return this.executeUploadJobPayload(job, originTabId);
  }

  async executeUploadJobPayload(job, originTabId = 0) {
    const clientId = await this.deps.getClientId();
    const adapter = getPlatformAdapter(job.platform);

    if (job.status !== 'running') {
      await this.deps.backend.updateUploadJobResult(job.job_id, {
        client_id: clientId,
        status: 'running',
        message: `${adapter.displayName} 上传任务已被浏览器扩展接管。`,
        current_url: '',
        error: '',
        result_payload: { phase: 'claimed' },
      });
    }
    await this.deps.notifyPage(originTabId, 'upload-status', {
      jobId: job.job_id,
      status: 'running',
      platform: job.platform,
      message: `${adapter.displayName} 上传任务执行中。`,
    });

    try {
      const targetUrl = job.upload_url || adapter.publishUrl;
      const tab = await this.deps.openUploadTab(targetUrl);
      const result = await this.deps.runUploadCommand(tab.tabId, {
        platform: job.platform,
        display_name: job.display_name,
        book_name: job.book_name,
        chapter_title: job.chapter_title,
        body: job.body,
        publish: job.publish,
      });
      const finalStatus = result.ok ? 'succeeded' : 'failed';
      await this.deps.backend.updateUploadJobResult(job.job_id, {
        client_id: clientId,
        status: finalStatus,
        message: result.message || (result.ok ? '上传已完成。' : '上传失败。'),
        current_url: result.currentUrl || '',
        error: result.error || '',
        result_payload: result.resultPayload || {},
      });

      await this.deps.setPlatformState(job.platform, {
        connected: finalStatus === 'succeeded' ? true : !String(result.currentUrl || '').includes('login'),
        loginMethod: 'scan',
        lastError: result.error || '',
      });
      await this.sendHeartbeat();
      await this.syncConnectedSessionsToBackend();
      await this.deps.notifyPage(originTabId, 'upload-status', {
        jobId: job.job_id,
        status: finalStatus,
        platform: job.platform,
        message: result.message || (result.ok ? '上传已完成。' : '上传失败。'),
      });
      return {
        message: result.ok ? '浏览器扩展已完成上传。' : '上传失败，请查看任务状态。',
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.deps.backend.updateUploadJobResult(job.job_id, {
        client_id: clientId,
        status: 'failed',
        message: '浏览器扩展执行上传任务时失败。',
        current_url: '',
        error: message,
        result_payload: { phase: 'controller-error' },
      });
      await this.deps.notifyPage(originTabId, 'upload-status', {
        jobId: job.job_id,
        status: 'failed',
        platform: job.platform,
        message,
      });
      throw error;
    }
  }

  async handleTabUpdated(tabId, changeInfo, tab) {
    const session = this.loginSessions.get(tabId);
    if (!session) {
      return;
    }
    const url = String(changeInfo?.url || tab?.url || session.lastUrl || '');
    session.lastUrl = url;
    await this.evaluateLoginSession(session, url);
  }

  async handleTabRemoved(tabId) {
    const session = this.loginSessions.get(tabId);
    if (!session) {
      return;
    }
    this.loginSessions.delete(tabId);
    await this.deps.setPlatformState(session.platformId, {
      connected: false,
      loginMethod: 'scan',
      lastError: '',
    });
    await this.sendHeartbeat();
    await this.deps.notifyPage(session.originTabId, 'login-status', {
      platform: session.platformId,
      connected: false,
      message: '登录弹窗已关闭，但还没有确认登录成功。',
    });
  }

  async handleCookieChanged() {
    const sessions = Array.from(this.loginSessions.values());
    for (const session of sessions) {
      const tab = await this.deps.getTab(session.popupTabId);
      if (!tab) {
        continue;
      }
      await this.evaluateLoginSession(session, tab.url || session.lastUrl || '');
    }
  }

  async sendHeartbeat() {
    const settings = await this.deps.getSettings();
    if (!settings.backendBaseUrl || !settings.apiKey) {
      return { skipped: true };
    }
    const clientId = await this.deps.getClientId();
    const browserInfo = await this.deps.getBrowserInfo();
    const platforms = [];
    for (const platformId of Object.keys(PLATFORM_ADAPTERS)) {
      const cookies = await this.deps.getCookies(platformId);
      const savedState = await this.deps.getPlatformState(platformId);
      const heartbeatState = buildHeartbeatState(platformId, cookies, savedState);
      platforms.push({
        ...heartbeatState,
        raw_state: {
          ...heartbeatState.raw_state,
          cookie_count: cookies.length,
        },
      });
    }
    return this.deps.backend.heartbeat({
      client_id: clientId,
      extension_version: this.deps.getExtensionVersion(),
      browser_name: browserInfo.browserName,
      browser_version: browserInfo.browserVersion,
      backend_base_url: settings.backendBaseUrl,
      platforms,
    });
  }

  async syncConnectedSessionsToBackend() {
    const settings = await this.deps.getSettings();
    if (!settings.backendBaseUrl || !settings.apiKey || !settings.syncSessionToBackend) {
      return { skipped: true };
    }
    const clientId = await this.deps.getClientId();
    let synced = 0;
    for (const platformId of Object.keys(PLATFORM_ADAPTERS)) {
      const savedState = await this.deps.getPlatformState(platformId);
      const cookies = await this.deps.getCookies(platformId);
      const heartbeatState = buildHeartbeatState(platformId, cookies, savedState);
      if (!heartbeatState.connected && !heartbeatState.raw_state?.cookie_signal) {
        continue;
      }
      if (!cookies.length) {
        continue;
      }
      await this.deps.backend.syncBrowserSession({
        client_id: clientId,
        platform: platformId,
        cookies: cookies.map((cookie) => this.#cookieForSessionSync(cookie)),
      });
      synced += 1;
    }
    return { synced };
  }

  async dispatchPendingUploadJobs() {
    if (this.dispatchInFlight) {
      return this.dispatchInFlight;
    }
    this.dispatchInFlight = this._dispatchPendingUploadJobs();
    try {
      return await this.dispatchInFlight;
    } finally {
      this.dispatchInFlight = null;
    }
  }

  async _dispatchPendingUploadJobs() {
    const settings = await this.deps.getSettings();
    if (!settings.backendBaseUrl || !settings.apiKey) {
      return { skipped: true };
    }

    const clientId = await this.deps.getClientId();
    const connectedPlatforms = [];
    for (const platformId of Object.keys(PLATFORM_ADAPTERS)) {
      const savedState = await this.deps.getPlatformState(platformId);
      const cookies = await this.deps.getCookies(platformId);
      const heartbeatState = buildHeartbeatState(platformId, cookies, savedState);
      if (heartbeatState.connected || heartbeatState.raw_state?.cookie_signal) {
        connectedPlatforms.push(platformId);
      }
    }
    if (!connectedPlatforms.length) {
      return { skipped: true };
    }
    let handled = 0;
    const MAX_JOBS_PER_DISPATCH = 8;
    while (handled < MAX_JOBS_PER_DISPATCH) {
      const claimed = await this.deps.backend.claimNextUploadJob({
        client_id: clientId,
        connected_platforms: connectedPlatforms,
      });
      if (!claimed?.found || !claimed.job) {
        return handled ? { found: true, handled } : { found: false };
      }
      handled += 1;
      await this.executeUploadJobPayload(claimed.job, 0);
    }
    return { found: true, handled, truncated: true };
  }

  async closeExistingPlatformSession(platformId) {
    const sessions = Array.from(this.loginSessions.values()).filter(
      (item) => item.platformId === platformId,
    );
    for (const session of sessions) {
      await this.deps.closePopup(session.popupWindowId);
      this.loginSessions.delete(session.popupTabId);
    }
  }

  #cookieForSessionSync(cookie) {
    return {
      name: String(cookie?.name || ''),
      value: String(cookie?.value || ''),
      domain: String(cookie?.domain || ''),
      path: String(cookie?.path || '/') || '/',
      secure: Boolean(cookie?.secure),
      httpOnly: Boolean(cookie?.httpOnly),
      sameSite: String(cookie?.sameSite || 'Lax'),
      expirationDate: cookie?.expirationDate ?? null,
    };
  }

  async evaluateLoginSession(session, url) {
    const cookies = await this.deps.getCookies(session.platformId);
    const inspection = await this.deps.inspectLoginState(session.popupTabId).catch(() => null);

    if (inspection?.authenticated) {
      await this.deps.setPlatformState(session.platformId, {
        connected: true,
        loginMethod: 'scan',
        lastError: '',
      });
      await this.sendHeartbeat();
      await this.syncConnectedSessionsToBackend();
      await this.deps.notifyPage(session.originTabId, 'login-status', {
        platform: session.platformId,
        connected: true,
        message: `${getPlatformAdapter(session.platformId).displayName} 登录成功，正在关闭弹窗。`,
      });
      await this.deps.closePopup(session.popupWindowId);
      this.loginSessions.delete(session.popupTabId);
      return;
    }

    if (shouldProbeLogin(session.platformId, { url, cookies, probeIndex: session.probeIndex }) && !inspection?.loginVisible) {
      const probeUrl = getProbeUrl(session.platformId, session.probeIndex);
      if (probeUrl) {
        session.probeIndex += 1;
        await this.deps.navigateTab(session.popupTabId, probeUrl);
        await this.deps.notifyPage(session.originTabId, 'login-status', {
          platform: session.platformId,
          connected: false,
          message: '检测到扫码已完成，正在主动确认作者后台登录状态...',
        });
      }
    }

    if (inspection && !inspection.authenticated) {
      await this.deps.notifyPage(session.originTabId, 'login-status', {
        platform: session.platformId,
        connected: false,
        message: inspection.loginVisible
          ? '已打开登录页，请继续扫码或完成登录。'
          : '正在等待平台确认作者后台登录状态...',
      });
    }
  }
}
