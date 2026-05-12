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
    this.commentDispatchInFlight = null;
    this.executionTasks = new Map();
    this.executionTabToTask = new Map();
  }

  async bootstrap() {
    await this.deps.ensureClientId();
    await this.deps.ensureHeartbeatAlarm();
    await this.restoreDisconnectedSessionsFromBackend();
    await this._syncBackendStateAndDispatchPendingJobs();
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
      await this.restoreDisconnectedSessionsFromBackend();
      await this._syncBackendStateAndDispatchPendingJobs();
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

  async restoreDisconnectedSessionsFromBackend() {
    const settings = await this.deps.getSettings();
    if (!settings.backendBaseUrl || !settings.apiKey) {
      return { skipped: true };
    }
    let restored = 0;
    for (const platformId of Object.keys(PLATFORM_ADAPTERS)) {
      const session = await this.deps.backend.getBrowserSession(platformId);
      if (!session?.cookies?.length) {
        continue;
      }
      await this.deps.setCookies(platformId, session.cookies);
      restored += 1;
    }
    return { restored };
  }

  async openLogin(platformId, originTabId) {
    const adapter = getPlatformAdapter(platformId);
    await this.closeExistingPlatformSession(platformId);
    await this.deps.setPlatformState(platformId, {
      connected: false,
      loginMethod: 'scan',
      lastError: '',
    });
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
    let syncWarning = '';
    try {
      await this.sendHeartbeat();
    } catch (error) {
      syncWarning = error instanceof Error ? error.message : String(error);
    }
    await this.deps.notifyPage(originTabId, 'login-status', {
      platform: platformId,
      connected: false,
      message: syncWarning
        ? `${adapter.displayName} 登录弹窗已打开，请在弹窗里完成扫码。状态同步稍后重试：${syncWarning}`
        : `${adapter.displayName} 登录弹窗已打开，请在弹窗里完成扫码。`,
    });
    return {
      message: syncWarning
        ? `${adapter.displayName} 登录弹窗已打开，但状态同步失败：${syncWarning}`
        : `${adapter.displayName} 登录弹窗已打开。`,
    };
  }

  async executeUploadJob(jobId, originTabId) {
    if (!jobId) {
      throw new Error('缺少上传任务 ID。');
    }
    const job = await this.deps.backend.getUploadJob(jobId);
    return this.executeUploadJobPayload(job, originTabId);
  }

  async executeCommentSyncJob(jobId, originTabId) {
    if (!jobId) {
      throw new Error('缺少评论同步任务 ID。');
    }
    const job = await this.deps.backend.getCommentSyncJob(jobId);
    return this.executeCommentSyncJobPayload(job, originTabId);
  }

  registerExecutionTask(taskKey, tabId) {
    if (!taskKey || !tabId) {
      return;
    }
    this.executionTasks.set(taskKey, { tabs: new Set([tabId]) });
    this.executionTabToTask.set(tabId, taskKey);
  }

  linkExecutionTab(tabId, openerTabId) {
    if (!tabId || !openerTabId) {
      return;
    }
    const taskKey = this.executionTabToTask.get(openerTabId);
    if (!taskKey) {
      return;
    }
    const task = this.executionTasks.get(taskKey);
    if (!task) {
      return;
    }
    task.tabs.add(tabId);
    this.executionTabToTask.set(tabId, taskKey);
  }

  unlinkExecutionTab(tabId) {
    if (!tabId) {
      return;
    }
    const taskKey = this.executionTabToTask.get(tabId);
    if (!taskKey) {
      return;
    }
    this.executionTabToTask.delete(tabId);
    const task = this.executionTasks.get(taskKey);
    if (!task) {
      return;
    }
    task.tabs.delete(tabId);
    if (!task.tabs.size) {
      this.executionTasks.delete(taskKey);
    }
  }

  async cleanupExecutionTabs(taskKey) {
    const task = this.executionTasks.get(taskKey);
    if (!task) {
      return { attempted: false, closed_tab_ids: [], failed_tab_ids: [] };
    }
    const tabIds = Array.from(task.tabs).filter(Boolean);
    const closedTabIds = [];
    const failedTabIds = [];
    for (const tabId of tabIds) {
      try {
        await this.deps.closeTab(tabId);
        closedTabIds.push(tabId);
      } catch (_error) {
        failedTabIds.push(tabId);
      }
      this.unlinkExecutionTab(tabId);
    }
    this.executionTasks.delete(taskKey);
    return {
      attempted: true,
      closed_tab_ids: closedTabIds,
      failed_tab_ids: failedTabIds,
    };
  }

  forgetExecutionTask(taskKey) {
    const task = this.executionTasks.get(taskKey);
    if (!task) {
      return;
    }
    for (const tabId of task.tabs) {
      this.executionTabToTask.delete(tabId);
    }
    this.executionTasks.delete(taskKey);
  }

  async waitForOpenedUploadTab(tabId, platformId, timeoutMs = 6000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const tab = await this.deps.getTab(tabId);
      const url = String(tab?.url || '');
      const isExpectedUrl = platformId === 'qidian'
        ? (url.includes('write.qq.com') || url.includes('pcwrite.yuewen.com'))
        : url.includes('fanqienovel.com');
      if (isExpectedUrl && (!tab?.status || tab.status === 'complete')) {
        return true;
      }
      await new Promise((resolve) => globalThis.setTimeout(resolve, 400));
    }
    return false;
  }

  async runUploadWithPageReadyRetries(tabId, platformId, uploadPayload) {
    let result = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      result = await this.deps.runUploadCommand(tabId, uploadPayload);
      if (result.ok || !String(result.error || '').includes('平台页面没有准备好')) {
        return result;
      }
      await this.waitForOpenedUploadTab(tabId, platformId, 8000 + (attempt * 4000));
      await new Promise((resolve) => globalThis.setTimeout(resolve, 1500 + (attempt * 1000)));
    }
    return result || {
      ok: false,
      error: '平台页面没有准备好，无法执行上传。',
      currentUrl: '',
    };
  }

  async executeUploadJobPayload(job, originTabId = 0) {
    const clientId = await this.deps.getClientId();
    const adapter = getPlatformAdapter(job.platform);
    const taskKey = `upload:${job.job_id}`;
    const refreshJob = async () => {
      if (typeof this.deps.backend?.getUploadJob !== 'function') {
        return job;
      }
      try {
        const latest = await this.deps.backend.getUploadJob(job.job_id);
        return latest && latest.job_id ? { ...job, ...latest } : job;
      } catch (_error) {
        return job;
      }
    };
    const cancelUpload = async (phase, currentUrl = '') => {
      this.forgetExecutionTask(taskKey);
      await this.deps.backend.updateUploadJobResult(job.job_id, {
        client_id: clientId,
        status: 'cancelled',
        message: '浏览器扩展已响应终止请求，上传任务已取消。',
        current_url: currentUrl,
        error: '',
        result_payload: { phase },
      });
      await this.deps.notifyPage(originTabId, 'upload-status', {
        jobId: job.job_id,
        status: 'cancelled',
        platform: job.platform,
        message: '上传任务已取消。',
      });
      return { message: '浏览器扩展已取消上传任务。' };
    };
    const createAbortWatcher = (pollMs = 1000) => {
      let active = true;
      let timerId = null;
      let resumeWait = null;
      const promise = new Promise((resolve) => {
        const loop = async () => {
          while (active) {
            const latestJob = await refreshJob();
            if (latestJob.abort_requested || latestJob.status === 'terminating' || latestJob.status === 'cancelled') {
              active = false;
              await this.cleanupExecutionTabs(taskKey).catch(() => ({ attempted: false }));
              resolve({
                __forwinAborted: true,
                currentUrl: '',
              });
              return;
            }
            await new Promise((resume) => {
              resumeWait = resume;
              timerId = globalThis.setTimeout(() => {
                timerId = null;
                const wake = resumeWait;
                resumeWait = null;
                if (wake) {
                  wake();
                }
              }, pollMs);
            });
          }
        };
        loop().catch(() => resolve({ __forwinAbortWatcherFailed: true }));
      });
      return {
        promise,
        stop() {
          active = false;
          if (timerId) {
            globalThis.clearTimeout(timerId);
            timerId = null;
          }
          if (resumeWait) {
            const wake = resumeWait;
            resumeWait = null;
            wake();
          }
        },
      };
    };
    const initialJob = Object.prototype.hasOwnProperty.call(job || {}, 'status') ? job : await refreshJob();
    if (initialJob.abort_requested || initialJob.status === 'terminating' || initialJob.status === 'cancelled') {
      return cancelUpload('abort-before-start');
    }

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
      this.registerExecutionTask(taskKey, tab.tabId);
      await this.waitForOpenedUploadTab(tab.tabId, job.platform, 6000);
      const openedTab = await this.deps.getTab(tab.tabId);
      const latestJob = await refreshJob();
      if (latestJob.abort_requested || latestJob.status === 'terminating' || latestJob.status === 'cancelled') {
        await this.cleanupExecutionTabs(taskKey);
        return cancelUpload('abort-before-execute', String(openedTab?.url || targetUrl || ''));
      }
      await this.deps.backend.updateUploadJobResult(job.job_id, {
        client_id: clientId,
        status: 'running',
        message: `${adapter.displayName} 正在打开平台编辑页。`,
        current_url: String(openedTab?.url || targetUrl || ''),
        error: '',
        result_payload: {
          ...(job.result_payload || {}),
          phase: 'opened-upload-tab',
        },
      });
      const uploadPayload = {
        platform: job.platform,
        display_name: job.display_name,
        book_name: job.book_name,
        chapter_title: job.chapter_title,
        body: job.body,
        publish: job.publish,
        create_if_missing: Boolean(job.result_payload?.create_if_missing),
        book_meta: job.result_payload?.book_meta || null,
      };
      const abortWatcher = createAbortWatcher(1000);
      let timeoutId = null;
      const timedResult = await Promise.race([
        this.runUploadWithPageReadyRetries(tab.tabId, job.platform, uploadPayload)
          .catch((error) => ({ __forwinUploadError: error })),
        new Promise((resolve) => {
          timeoutId = globalThis.setTimeout(() => resolve({ __forwinTimedOut: true }), 90000);
        }),
        abortWatcher.promise,
      ]);
      abortWatcher.stop();
      if (timeoutId) {
        globalThis.clearTimeout(timeoutId);
      }
      if (timedResult?.__forwinAborted) {
        return cancelUpload('abort-during-execute', String((await this.deps.getTab(tab.tabId))?.url || ''));
      }
      if (timedResult?.__forwinUploadError) {
        throw timedResult.__forwinUploadError;
      }
      const result = timedResult?.__forwinTimedOut
        ? {
          ok: false,
          currentUrl: String((await this.deps.getTab(tab.tabId))?.url || ''),
          error: '浏览器扩展执行超时，未能完成平台章节流程。',
          errorCode: 'extension-upload-timeout',
          resultPayload: {
            phase: 'execute-upload-timeout',
          },
        }
        : timedResult;
      const finalStatus = result.ok ? 'succeeded' : 'failed';
      const cleanupPayload = result.ok ? await this.cleanupExecutionTabs(taskKey) : { attempted: false };
      if (!result.ok) {
        this.forgetExecutionTask(taskKey);
      }
      const resultPayload = {
        ...(result.resultPayload || {}),
        ...(result.errorCode ? { error_code: result.errorCode } : {}),
        tab_cleanup: cleanupPayload,
      };
      await this.deps.backend.updateUploadJobResult(job.job_id, {
        client_id: clientId,
        status: finalStatus,
        message: result.message || (result.ok ? '上传已完成。' : '上传失败。'),
        current_url: result.currentUrl || '',
        error: result.error || '',
        result_payload: resultPayload,
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
      this.forgetExecutionTask(taskKey);
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

  async executeCommentSyncJobPayload(job, originTabId = 0) {
    const clientId = await this.deps.getClientId();
    const adapter = getPlatformAdapter(job.platform);
    const taskKey = `comment:${job.job_id}`;

    await this.deps.backend.updateCommentSyncJobResult(job.job_id, {
      client_id: clientId,
      status: 'running',
      message: `${adapter.displayName} 评论同步任务已被浏览器扩展接管。`,
      error: '',
      result_payload: { phase: 'claimed' },
    });
    await this.deps.notifyPage(originTabId, 'comment-sync-status', {
      jobId: job.job_id,
      status: 'running',
      platform: job.platform,
      message: `${adapter.displayName} 评论同步执行中。`,
    });

    try {
      const targetUrl = job.comment_url || adapter.commentUrl || adapter.dashboardUrl || adapter.publishUrl;
      const tab = await this.deps.openUploadTab(targetUrl);
      this.registerExecutionTask(taskKey, tab.tabId);
      await this.waitForOpenedUploadTab(tab.tabId, job.platform, 8000);
      const openedTab = await this.deps.getTab(tab.tabId);
      const syncPayload = {
        platform: job.platform,
        work_id: job.work_id,
        work_name: job.work_name,
        chapter_id: job.chapter_id,
        chapter_title: job.chapter_title,
        limit: Number(job.limit || 0) || 100,
      };
      const result = await this.deps.runCommentSyncCommand(tab.tabId, syncPayload);
      if (!result?.ok) {
        this.forgetExecutionTask(taskKey);
        await this.deps.backend.updateCommentSyncJobResult(job.job_id, {
          client_id: clientId,
          status: 'failed',
          message: result?.message || '评论同步失败。',
          error: result?.error || '平台未返回评论数据。',
          result_payload: {
            ...(result?.resultPayload || {}),
            current_url: result?.currentUrl || String(openedTab?.url || targetUrl || ''),
          },
        });
        return {
          message: '评论同步失败，请查看任务状态。',
        };
      }

      const comments = Array.isArray(result.comments) ? result.comments : [];
      const uploadResult = await this.deps.backend.syncCommentsBatch({
        client_id: clientId,
        platform: job.platform,
        job_id: job.job_id,
        comments,
      });
      const cleanupPayload = await this.cleanupExecutionTabs(taskKey);
      await this.deps.backend.updateCommentSyncJobResult(job.job_id, {
        client_id: clientId,
        status: 'succeeded',
        message: result.message || '评论同步已完成。',
        error: '',
        result_payload: {
          ...(result.resultPayload || {}),
          fetched_count: comments.length,
          inserted: Number(uploadResult?.inserted || 0),
          updated: Number(uploadResult?.updated || 0),
          current_url: result.currentUrl || String(openedTab?.url || targetUrl || ''),
          tab_cleanup: cleanupPayload,
        },
      });
      await this.deps.notifyPage(originTabId, 'comment-sync-status', {
        jobId: job.job_id,
        status: 'succeeded',
        platform: job.platform,
        message: result.message || '评论同步已完成。',
      });
      return {
        message: result.message || '浏览器扩展已完成评论同步。',
      };
    } catch (error) {
      this.forgetExecutionTask(taskKey);
      const message = error instanceof Error ? error.message : String(error);
      await this.deps.backend.updateCommentSyncJobResult(job.job_id, {
        client_id: clientId,
        status: 'failed',
        message: '浏览器扩展执行评论同步任务时失败。',
        error: message,
        result_payload: { phase: 'controller-error' },
      });
      await this.deps.notifyPage(originTabId, 'comment-sync-status', {
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

  async handleTabCreated(tab) {
    const tabId = Number(tab?.id || 0);
    const openerTabId = Number(tab?.openerTabId || 0);
    this.linkExecutionTab(tabId, openerTabId);
  }

  async handleTabRemoved(tabId) {
    this.unlinkExecutionTab(tabId);
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

  async _syncBackendStateAndDispatchPendingJobs() {
    await this.sendHeartbeat();
    await this.syncConnectedSessionsToBackend();
    await this.dispatchPendingUploadJobs();
    await this.dispatchPendingCommentSyncJobs();
  }

  async inspectPlatformState(platformId) {
    if (typeof this.deps.inspectPlatformState !== 'function') {
      return null;
    }
    try {
      return await this.deps.inspectPlatformState(platformId);
    } catch (_error) {
      return null;
    }
  }

  async _collectConnectedPlatforms() {
    const connectedPlatforms = [];
    for (const platformId of Object.keys(PLATFORM_ADAPTERS)) {
      const savedState = await this.deps.getPlatformState(platformId);
      const cookies = await this.deps.getCookies(platformId);
      const inspection = await this.inspectPlatformState(platformId);
      const heartbeatState = buildHeartbeatState(platformId, cookies, savedState, inspection);
      const loggedOutByPage = heartbeatState.raw_state?.page_login_visible
        && !heartbeatState.raw_state?.page_authenticated;
      if (!loggedOutByPage && (heartbeatState.connected || heartbeatState.raw_state?.cookie_signal)) {
        connectedPlatforms.push(platformId);
      }
    }
    return connectedPlatforms;
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
      const inspection = await this.inspectPlatformState(platformId);
      const heartbeatState = buildHeartbeatState(platformId, cookies, savedState, inspection);
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
      const inspection = await this.inspectPlatformState(platformId);
      const heartbeatState = buildHeartbeatState(platformId, cookies, savedState, inspection);
      const loggedOutByPage = heartbeatState.raw_state?.page_login_visible
        && !heartbeatState.raw_state?.page_authenticated;
      if (loggedOutByPage || (!heartbeatState.connected && !heartbeatState.raw_state?.cookie_signal)) {
        continue;
      }
      if (!cookies.length) {
        continue;
      }
      await this.deps.backend.syncBrowserSession({
        client_id: clientId,
        platform: platformId,
        raw_state: {
          ...heartbeatState.raw_state,
          connected: heartbeatState.connected,
          login_method: heartbeatState.login_method,
          last_error: heartbeatState.last_error,
          cookie_count: cookies.length,
        },
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

  async dispatchPendingCommentSyncJobs() {
    if (this.commentDispatchInFlight) {
      return this.commentDispatchInFlight;
    }
    this.commentDispatchInFlight = this._dispatchPendingCommentSyncJobs();
    try {
      return await this.commentDispatchInFlight;
    } finally {
      this.commentDispatchInFlight = null;
    }
  }

  async _dispatchPendingJobs({ claimJob, executeJob }) {
    const settings = await this.deps.getSettings();
    if (!settings.backendBaseUrl || !settings.apiKey) {
      return { skipped: true };
    }

    const clientId = await this.deps.getClientId();
    const connectedPlatforms = await this._collectConnectedPlatforms();
    if (!connectedPlatforms.length) {
      return { skipped: true };
    }
    let handled = 0;
    const MAX_JOBS_PER_DISPATCH = 8;
    while (handled < MAX_JOBS_PER_DISPATCH) {
      const claimed = await claimJob({
        client_id: clientId,
        connected_platforms: connectedPlatforms,
      });
      if (!claimed?.found || !claimed.job) {
        return handled ? { found: true, handled } : { found: false };
      }
      handled += 1;
      await executeJob(claimed.job);
    }
    return { found: true, handled, truncated: true };
  }

  async _dispatchPendingUploadJobs() {
    return this._dispatchPendingJobs({
      claimJob: (payload) => this.deps.backend.claimNextUploadJob(payload),
      executeJob: (job) => this.executeUploadJobPayload(job, 0),
    });
  }

  async _dispatchPendingCommentSyncJobs() {
    const settings = await this.deps.getSettings();
    if (
      !settings.backendBaseUrl
      || !settings.apiKey
      || typeof this.deps.backend?.claimNextCommentSyncJob !== 'function'
    ) {
      return { skipped: true };
    }
    return this._dispatchPendingJobs({
      claimJob: (payload) => this.deps.backend.claimNextCommentSyncJob(payload),
      executeJob: (job) => this.executeCommentSyncJobPayload(job, 0),
    });
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
      await this._syncBackendStateAndDispatchPendingJobs();
      await this.deps.notifyPage(session.originTabId, 'login-status', {
        platform: session.platformId,
        connected: true,
        message: `${getPlatformAdapter(session.platformId).displayName} 登录成功，正在关闭弹窗。`,
      });
      this.loginSessions.delete(session.popupTabId);
      await this.deps.closePopup(session.popupWindowId);
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
