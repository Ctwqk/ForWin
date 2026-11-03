import {
  PLATFORM_ADAPTERS,
  buildHeartbeatState,
  getPlatformAdapter,
  getProbeUrl,
  shouldProbeLogin,
} from './platforms.js?v=0.1.66';
import {
  buildAttemptResult,
  buildExecutionReceipt,
  buildReconciliationRequest,
  buildRiskPauseRequest,
  isRiskReason,
} from './reconciliation.js?v=0.1.66';
import { guardRiskInspection } from './risk-inspection.js?v=0.1.66';
import { HUB_BACKEND, HubExecutor } from './hub-adapter.js?v=0.1.66';

const LOGIN_QR_NOTIFICATION_THROTTLE_MS = 2 * 60_000;
const LOGIN_QR_PLATFORM_THROTTLE_URL = '__platform__';
const MAX_AUDIT_REQUESTS_PER_DISPATCH = 8;
// What an upload or comment sync dispatch answers while an audit round is running: one
// dispatcher at a time, and the audit tab takes the browser's focus. The next round (or the
// next trigger) tries again; waiting here would hold up the options page and login flows.
const AUDIT_RUNNING = Object.freeze({ skipped: true, reason: 'audit-running' });

function defaultNowMs() {
  return Date.now();
}

function loginQrNotificationsAllowed(settings, nowMs) {
  const allowedUntilMs = Number(settings?.loginQrNotificationsAllowedUntilMs || 0);
  return settings?.loginQrNotificationsEnabled === true
    && settings?.loginQrNotificationsAllowed === true
    && Number.isFinite(allowedUntilMs)
    && allowedUntilMs > nowMs;
}

function loginQrNotificationsDisabled(result) {
  if (result?.disabled) {
    return true;
  }
  if (result?.ok && result?.dispatched === false && !result?.throttled) {
    return true;
  }
  const message = String(result?.message || '').toLowerCase();
  return message.includes('webhook is not configured');
}

function isLoginRequiredError(value) {
  return ['login-required', 'platform-login-required'].includes(
    String(value || '').trim().toLowerCase(),
  );
}

export class PublisherExtensionController {
  constructor(deps) {
    this.deps = deps;
    this.loginSessions = new Map();
    this.dispatchInFlight = null;
    this.commentDispatchInFlight = null;
    this.auditDispatchInFlight = null;
    this.executionTasks = new Map();
    this.executionTabToTask = new Map();
    this.heartbeatLoginQrNotificationSessions = new Map();
    this.hubExecutor = null;
  }

  // The publish-hub executor, when the extension was given a hub client. It is used
  // only while the hub URL and key are both set (see _dispatchPendingUploadJobs).
  _hubExecutor() {
    if (!this.deps.hubBackend) {
      return null;
    }
    if (!this.hubExecutor) {
      this.hubExecutor = new HubExecutor(this, this.deps);
    }
    return this.hubExecutor;
  }

  _hubEnabled(settings) {
    const hub = this._hubExecutor();
    return hub && hub.isConfigured(settings) ? hub : null;
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
    if (action === 'hub-client-id') {
      // The options page shows it so it can go into publish-hub's
      // create-account --preferred-client-id before any job runs. Generated on first
      // request, the same id the publish-hub adapter uses.
      const hubClientId = typeof this.deps.getHubClientId === 'function'
        ? String(await this.deps.getHubClientId())
        : '';
      return { hubClientId };
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
    if (!tabId) {
      return;
    }
    let taskKey = this.executionTabToTask.get(openerTabId);
    if (!taskKey && this.executionTasks.size === 1) {
      [taskKey] = this.executionTasks.keys();
    }
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

  _requireUploadJournal() {
    if (!this.deps.uploadJournal) {
      throw new Error('Upload journal is not configured.');
    }
    return this.deps.uploadJournal;
  }

  _attemptCoordinates(entry) {
    const job = entry?.job || {};
    const attempt = entry?.attempt || {};
    return {
      job,
      attempt,
      jobId: String(job.job_id || ''),
      attemptId: String(attempt.attempt_id || ''),
      fence: {
        client_id: String(entry?.client_id || ''),
        lease_epoch: Number(attempt.lease_epoch || 0),
      },
    };
  }

  _isStaleAttemptError(error) {
    return ['stale_attempt', 'lease_expired'].includes(String(error?.code || ''));
  }

  _stalePauseCoverage(error) {
    if (!this._isStaleAttemptError(error)) {
      return '';
    }
    const detail = error?.payload?.detail;
    const jobStatus = String(
      detail?.job_status || error?.payload?.job_status || '',
    );
    return ['succeeded', 'failed', 'cancelled'].includes(jobStatus) ? jobStatus : '';
  }

  _attemptMustStop(response) {
    return Boolean(response?.abort_requested || response?.next_action === 'stop');
  }

  async _callAttemptBackend(entry, method, payload = {}) {
    const { jobId, attemptId, fence } = this._attemptCoordinates(entry);
    return this.deps.backend[method](jobId, attemptId, { ...fence, ...payload });
  }

  _startUploadAttemptHeartbeat(entry, taskKey) {
    const intervalSeconds = Math.max(
      1,
      Number(entry?.attempt?.heartbeat_interval_seconds || 30),
    );
    let active = true;
    let blocked = false;
    let blockReason = '';
    let timerId = null;
    const stopExecution = async (reason) => {
      blocked = true;
      blockReason = reason;
      if (taskKey) {
        await this.cleanupExecutionTabs(taskKey).catch(() => ({ attempted: false }));
      }
    };
    const schedule = () => {
      if (!active || blocked) {
        return;
      }
      timerId = globalThis.setTimeout(async () => {
        try {
          const response = await this._callAttemptBackend(entry, 'heartbeatUploadAttempt');
          if (this._attemptMustStop(response)) {
            await stopExecution('abort-requested');
          }
        } catch (error) {
          await stopExecution(
            Number(error?.status || 0) === 409
              ? 'attempt-fence-rejected'
              : 'attempt-heartbeat-failed',
          );
        } finally {
          schedule();
        }
      }, intervalSeconds * 1000);
      timerId?.unref?.();
    };
    schedule();
    return {
      isBlocked: () => blocked,
      reason: () => blockReason,
      stop() {
        active = false;
        if (timerId) {
          globalThis.clearTimeout(timerId);
          timerId = null;
        }
      },
    };
  }

  _uploadTargetUrl(job) {
    const adapter = getPlatformAdapter(job.platform);
    return String(
      job.input?.upload_url
      || job.input?.remote_url
      || adapter.dashboardUrl
      || adapter.publishUrl
      || '',
    );
  }

  async _notifyUploadAttempt(originTabId, entry, status, message) {
    await this.deps.notifyPage(originTabId, 'upload-status', {
      jobId: entry.job.job_id,
      attemptId: entry.attempt.attempt_id,
      status,
      platform: entry.job.platform,
      message,
    });
  }

  async _submitStoredResult(entry) {
    if (entry.execution_mode === 'reconcile' && entry.job.task_kind !== 'audit_sync') {
      return this._callAttemptBackend(entry, 'reconcileUploadAttempt', entry.result);
    }
    return this._callAttemptBackend(entry, 'submitUploadAttemptResult', entry.result);
  }

  async _submitStoredPause(entry) {
    return this._callAttemptBackend(entry, 'pauseUploadAttempt', entry.pause);
  }

  async _markJournalAcknowledged(attemptId) {
    const journal = this._requireUploadJournal();
    await journal.markAcknowledged(attemptId);
    await journal.compact();
  }

  async _saveJournalResult(attemptId, result) {
    const journal = this._requireUploadJournal();
    await journal.saveResult(attemptId, result);
    return journal.get(attemptId);
  }

  _isRiskPauseSignal(signal) {
    return Boolean(signal?.riskPause || signal?.risk_pause);
  }

  async _persistAndReportRiskPause(entry, signal, originTabId) {
    const journal = this._requireUploadJournal();
    const pause = buildRiskPauseRequest({
      signal,
      observedAt: new Date().toISOString(),
    });
    await journal.savePause(entry.attempt.attempt_id, pause);
    entry = await journal.get(entry.attempt.attempt_id);
    let pauseState;
    try {
      pauseState = await this._submitStoredPause(entry);
    } catch (error) {
      const coveredStatus = this._stalePauseCoverage(error);
      if (!coveredStatus) {
        throw error;
      }
      pauseState = { job_status: coveredStatus };
    }
    await this._markJournalAcknowledged(entry.attempt.attempt_id);
    const finalStatus = pauseState?.job_status || 'paused';
    await this._notifyUploadAttempt(
      originTabId,
      entry,
      finalStatus,
      pause.evidence.message || `Publisher risk pause: ${pause.risk_reason}`,
    );
    return { status: finalStatus };
  }

  async _replayJournalEntry(rawEntry) {
    const journal = this._requireUploadJournal();
    let entry = await journal.get(rawEntry.attempt.attempt_id);
    if (entry.pause) {
      try {
        await this._submitStoredPause(entry);
      } catch (error) {
        if (!this._stalePauseCoverage(error)) {
          throw error;
        }
      }
      await this._markJournalAcknowledged(entry.attempt.attempt_id);
      return { acknowledged: true, recovered_by: 'risk_pause_replay' };
    }
    if (entry.receipt && !entry.result) {
      entry = await this._saveJournalResult(
        entry.attempt.attempt_id,
        buildAttemptResult({
          job: entry.job,
          result: {
            ok: true,
            message: 'Durable publisher receipt recovered after extension restart.',
            currentUrl: entry.receipt.remote_url,
            resultPayload: entry.job.task_kind === 'cover_upload'
              ? {
                cover_state: 'uploaded',
                platform_message: entry.receipt.evidence?.platform_message || '',
              }
              : {},
          },
        }),
      );
    }
    if (entry.receipt) {
      const receiptState = await this._callAttemptBackend(
        entry,
        'submitUploadReceipt',
        entry.receipt,
      );
      if (receiptState?.job_status === 'succeeded' && receiptState?.attempt_status !== 'running') {
        await this._markJournalAcknowledged(entry.attempt.attempt_id);
        return { acknowledged: true, recovered_by: 'late_receipt' };
      }
    }
    if (!entry.result) {
      if (entry.execution_mode === 'reconcile' && entry.job.task_kind !== 'audit_sync') {
        try {
          await this._callAttemptBackend(entry, 'updateUploadAttemptPhase', {
            phase: 'observation_started',
            current_url: '',
          });
        } catch (error) {
          if (!this._isStaleAttemptError(error)) {
            throw error;
          }
          await this._saveJournalResult(
            entry.attempt.attempt_id,
            buildReconciliationRequest({
              job: entry.job,
              observedAt: new Date().toISOString(),
              observation: {
                outcome: 'indeterminate',
                reason: 'Backend already retired the local reconciliation fence.',
              },
            }),
          );
          await this._markJournalAcknowledged(entry.attempt.attempt_id);
          return { acknowledged: true, recovered_by: 'stale_reconciliation_fence' };
        }
        entry = await this._saveJournalResult(
          entry.attempt.attempt_id,
          buildReconciliationRequest({
            job: entry.job,
            observedAt: new Date().toISOString(),
            observation: {
              outcome: 'indeterminate',
              reason: 'Extension worker restarted before read-only evidence was persisted.',
            },
          }),
        );
      } else {
        entry = await this._saveJournalResult(
          entry.attempt.attempt_id,
          buildAttemptResult({
            job: entry.job,
            result: {
              ok: false,
              errorCode: 'extension-worker-restarted',
              error: 'Extension worker restarted before a durable remote receipt was recorded.',
            },
          }),
        );
      }
    }
    try {
      await this._submitStoredResult(entry);
    } catch (error) {
      if (!this._isStaleAttemptError(error)) {
        throw error;
      }
    }
    await this._markJournalAcknowledged(entry.attempt.attempt_id);
    return { acknowledged: true, recovered_by: 'result_replay' };
  }

  async syncUploadJournal() {
    if (!this.deps.uploadJournal) {
      return { skipped: true, handled: 0 };
    }
    const journal = this._requireUploadJournal();
    await journal.load();
    // publish-hub attempts are replayed to publish-hub only (HubExecutor.replayJournal).
    const entries = (await journal.pending()).filter((entry) => entry.backend !== HUB_BACKEND);
    let handled = 0;
    for (const entry of entries) {
      await this._replayJournalEntry(entry);
      handled += 1;
    }
    await journal.compact();
    return { handled };
  }

  async _runMutationCommand(tabId, job) {
    const command = job.task_kind === 'cover_upload'
      ? this.deps.runCoverUploadCommand
      : this.deps.runUploadCommand;
    if (typeof command !== 'function') {
      throw new Error(`Missing browser command for ${job.task_kind}.`);
    }
    return command(tabId, {
      platform: job.platform,
      task_kind: job.task_kind,
      content_sha256: job.content_sha256,
      ...(job.input || {}),
    }).catch((error) => ({
      ok: false,
      errorCode: 'extension-command-failed',
      error: error instanceof Error ? error.message : String(error),
    }));
  }

  async _executeMutatingClaim(entry, originTabId) {
    const journal = this._requireUploadJournal();
    const taskKey = `upload:${entry.job.job_id}:${entry.attempt.attempt_id}`;
    const heartbeat = this._startUploadAttemptHeartbeat(entry, taskKey);
    try {
      await this._notifyUploadAttempt(originTabId, entry, 'running', '浏览器扩展正在执行发布任务。');
      const tab = await this.deps.openUploadTab(this._uploadTargetUrl(entry.job));
      this.registerExecutionTask(taskKey, tab.tabId);
      await this.waitForOpenedUploadTab(tab.tabId, entry.job.platform, 8000);
      const opened = await this.deps.getTab(tab.tabId);
      const riskSignal = await this.deps.inspectPlatformRiskCommand(tab.tabId, {
        platform: entry.job.platform,
        boundary: 'pre-mutation',
      });
      const riskBlocker = guardRiskInspection(riskSignal, 'pre-mutation');
      if (this._isRiskPauseSignal(riskBlocker)) {
        return this._persistAndReportRiskPause(entry, riskBlocker, originTabId);
      }
      if (riskBlocker) {
        const result = buildAttemptResult({ job: entry.job, result: riskBlocker });
        entry = await this._saveJournalResult(entry.attempt.attempt_id, result);
        const finalState = await this._submitStoredResult(entry);
        await this._markJournalAcknowledged(entry.attempt.attempt_id);
        await this._notifyUploadAttempt(
          originTabId,
          entry,
          finalState?.job_status || result.outcome,
          result.error_message,
        );
        return { status: finalState?.job_status || result.outcome };
      }
      await journal.markMutationStarted(entry.attempt.attempt_id);
      entry = await journal.get(entry.attempt.attempt_id);
      const phaseState = await this._callAttemptBackend(entry, 'updateUploadAttemptPhase', {
        phase: 'mutation_started',
        current_url: String(opened?.url || ''),
      });
      if (this._attemptMustStop(phaseState) || heartbeat.isBlocked()) {
        const cancelled = buildAttemptResult({
          job: entry.job,
          result: {
            outcome: 'cancelled',
            message: 'Publisher attempt stopped before the browser mutation command.',
            currentUrl: String(opened?.url || ''),
          },
        });
        entry = await this._saveJournalResult(entry.attempt.attempt_id, cancelled);
        await this._submitStoredResult(entry);
        await this._markJournalAcknowledged(entry.attempt.attempt_id);
        return { status: 'cancelled' };
      }

      let platformResult = await this._runMutationCommand(tab.tabId, entry.job);
      if (this._isRiskPauseSignal(platformResult)) {
        return this._persistAndReportRiskPause(entry, platformResult, originTabId);
      }
      if (heartbeat.isBlocked() && !platformResult?.ok) {
        throw new Error(`Publisher attempt stopped after mutation began: ${heartbeat.reason()}`);
      }
      if (platformResult?.ok) {
        try {
          const receipt = buildExecutionReceipt({
            job: entry.job,
            result: platformResult,
            observedAt: new Date().toISOString(),
          });
          await journal.saveReceipt(entry.attempt.attempt_id, receipt);
        } catch (error) {
          platformResult = {
            ok: false,
            currentUrl: platformResult?.currentUrl || '',
            errorCode: 'receipt-evidence-missing',
            error: error instanceof Error ? error.message : String(error),
          };
        }
      }
      const result = buildAttemptResult({ job: entry.job, result: platformResult });
      await journal.saveResult(entry.attempt.attempt_id, result);
      entry = await journal.get(entry.attempt.attempt_id);
      await this.cleanupExecutionTabs(taskKey);
      if (entry.receipt) {
        await this._callAttemptBackend(entry, 'submitUploadReceipt', entry.receipt);
      }
      const finalState = await this._submitStoredResult(entry);
      await this._markJournalAcknowledged(entry.attempt.attempt_id);
      await this._notifyUploadAttempt(
        originTabId,
        entry,
        finalState?.job_status || result.outcome,
        result.message || result.error_message,
      );
      return { status: finalState?.job_status || result.outcome };
    } finally {
      heartbeat.stop();
      await this.cleanupExecutionTabs(taskKey).catch(() => ({ attempted: false }));
    }
  }

  async _executeAuditClaim(entry, originTabId) {
    const taskKey = `audit:${entry.job.job_id}:${entry.attempt.attempt_id}`;
    const heartbeat = this._startUploadAttemptHeartbeat(entry, taskKey);
    try {
      const tab = await this.deps.openUploadTab(this._uploadTargetUrl(entry.job));
      this.registerExecutionTask(taskKey, tab.tabId);
      await this.waitForOpenedUploadTab(tab.tabId, entry.job.platform, 8000);
      const opened = await this.deps.getTab(tab.tabId);
      const phaseState = await this._callAttemptBackend(entry, 'updateUploadAttemptPhase', {
        phase: 'observation_started',
        current_url: String(opened?.url || ''),
      });
      if (this._attemptMustStop(phaseState) || heartbeat.isBlocked()) {
        throw new Error('Audit attempt was stopped before observation.');
      }
      const platformResult = await this.deps.runAuditSyncCommand(tab.tabId, {
        platform: entry.job.platform,
        task_kind: entry.job.task_kind,
        content_sha256: entry.job.content_sha256,
        ...(entry.job.input || {}),
      });
      if (this._isRiskPauseSignal(platformResult)) {
        return this._persistAndReportRiskPause(entry, platformResult, originTabId);
      }
      const result = buildAttemptResult({ job: entry.job, result: platformResult });
      entry = await this._saveJournalResult(entry.attempt.attempt_id, result);
      const finalState = await this._submitStoredResult(entry);
      await this._markJournalAcknowledged(entry.attempt.attempt_id);
      if (result.outcome === 'succeeded') {
        await this.cleanupExecutionTabs(taskKey);
      }
      await this._notifyUploadAttempt(
        originTabId,
        entry,
        finalState?.job_status || result.outcome,
        result.message || result.error_message,
      );
      return { status: finalState?.job_status || result.outcome };
    } finally {
      heartbeat.stop();
      await this.cleanupExecutionTabs(taskKey).catch(() => ({ attempted: false }));
    }
  }

  async _executeReconciliationClaim(entry, originTabId) {
    const taskKey = `reconcile:${entry.job.job_id}:${entry.attempt.attempt_id}`;
    const heartbeat = this._startUploadAttemptHeartbeat(entry, taskKey);
    try {
      const tab = await this.deps.openUploadTab(this._uploadTargetUrl(entry.job));
      this.registerExecutionTask(taskKey, tab.tabId);
      await this.waitForOpenedUploadTab(tab.tabId, entry.job.platform, 8000);
      const opened = await this.deps.getTab(tab.tabId);
      const phaseState = await this._callAttemptBackend(entry, 'updateUploadAttemptPhase', {
        phase: 'observation_started',
        current_url: String(opened?.url || ''),
      });
      if (this._attemptMustStop(phaseState) || heartbeat.isBlocked()) {
        throw new Error('Reconciliation attempt was stopped before observation.');
      }
      const observation = await this.deps.runReconciliationCommand(tab.tabId, {
        platform: entry.job.platform,
        task_kind: entry.job.task_kind,
        content_sha256: entry.job.content_sha256,
        ...(entry.job.input || {}),
      });
      const request = buildReconciliationRequest({
        job: entry.job,
        observation,
        observedAt: new Date().toISOString(),
      });
      entry = await this._saveJournalResult(entry.attempt.attempt_id, request);
      const finalState = await this._submitStoredResult(entry);
      await this._markJournalAcknowledged(entry.attempt.attempt_id);
      await this.cleanupExecutionTabs(taskKey);
      await this._notifyUploadAttempt(
        originTabId,
        entry,
        finalState?.job_status || request.outcome,
        request.error_message || `只读核对结果：${request.outcome}`,
      );
      return { status: finalState?.job_status || request.outcome };
    } finally {
      heartbeat.stop();
      await this.cleanupExecutionTabs(taskKey).catch(() => ({ attempted: false }));
    }
  }

  async executeUploadClaim(claim, originTabId = 0) {
    if (!claim?.job?.job_id || !claim?.attempt?.attempt_id) {
      throw new Error('Upload claim is missing its job or attempt fence.');
    }
    const journal = this._requireUploadJournal();
    const clientId = await this.deps.getClientId();
    let entry = await journal.recordClaim({ clientId, claim });
    entry = entry || await journal.get(claim.attempt.attempt_id);
    if (entry.local_phase !== 'claimed' || entry.receipt || entry.result || entry.pause) {
      return this._replayJournalEntry(entry);
    }
    if (entry.job.task_kind === 'audit_sync') {
      return this._executeAuditClaim(entry, originTabId);
    }
    if (entry.execution_mode === 'reconcile') {
      return this._executeReconciliationClaim(entry, originTabId);
    }
    return this._executeMutatingClaim(entry, originTabId);
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
    await this._dispatchCommentSyncThenAuditRequests();
  }

  async inspectPlatformState(platformId, context = {}) {
    let inspection = null;
    if (typeof this.deps.inspectPlatformState !== 'function') {
      inspection = null;
    } else {
      try {
        inspection = await this.deps.inspectPlatformState(platformId);
      } catch (_error) {
        inspection = null;
      }
    }
    const canProbeCookieSignal = Boolean(
      context?.probeCookieSignal
      && typeof this.deps.ensurePlatformProbeInspection === 'function',
    );
    if (!canProbeCookieSignal) {
      return inspection;
    }

    const cookies = Array.isArray(context.cookies) ? context.cookies : [];
    const savedState = context.savedState && typeof context.savedState === 'object'
      ? context.savedState
      : {};
    const provisionalState = buildHeartbeatState(platformId, cookies, savedState, inspection);
    if (!provisionalState.raw_state?.cookie_signal) {
      return inspection;
    }
    const loggedOutByInspectedLoginPage = Boolean(
      provisionalState.raw_state?.page_login_visible
      && !provisionalState.raw_state?.page_authenticated,
    );
    const hasConclusivePageEvidence = Boolean(
      inspection?.authenticated || inspection?.loginVisible,
    );
    if (inspection?.ok && hasConclusivePageEvidence && !loggedOutByInspectedLoginPage) {
      return inspection;
    }

    try {
      return await this.deps.ensurePlatformProbeInspection(platformId) || inspection;
    } catch (_error) {
      return inspection;
    }
  }

  async _collectConnectedPlatforms() {
    const connectedPlatforms = [];
    for (const platformId of Object.keys(PLATFORM_ADAPTERS)) {
      const savedState = await this.deps.getPlatformState(platformId);
      const cookies = await this.deps.getCookies(platformId);
      const inspection = await this.inspectPlatformState(platformId, {
        cookies,
        savedState,
        probeCookieSignal: true,
      });
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
      const inspection = await this.inspectPlatformState(platformId, {
        cookies,
        savedState,
        probeCookieSignal: true,
      });
      const heartbeatState = buildHeartbeatState(platformId, cookies, savedState, inspection);
      if (
        isLoginRequiredError(heartbeatState.last_error)
        && !isLoginRequiredError(savedState.lastError)
        && typeof this.deps.setPlatformState === 'function'
      ) {
        await this.deps.setPlatformState(platformId, {
          connected: false,
          loginMethod: heartbeatState.login_method || savedState.loginMethod || 'scan',
          lastError: heartbeatState.last_error,
        });
      }
      await this.recordHeartbeatPlatformState({
        platform: platformId,
        inspection_ok: Boolean(inspection?.ok),
        inspection_tab_id: Number(inspection?.tabId || inspection?.tab_id || 0),
        inspection_login_visible: Boolean(inspection?.loginVisible),
        inspection_authenticated: Boolean(inspection?.authenticated),
        inspection_current_url: String(inspection?.currentUrl || inspection?.url || ''),
        raw_page_login_visible: Boolean(heartbeatState.raw_state?.page_login_visible),
        raw_page_authenticated: Boolean(heartbeatState.raw_state?.page_authenticated),
        raw_current_url: String(heartbeatState.raw_state?.current_url || ''),
      });
      await this.maybeNotifyHeartbeatLoginQr(platformId, inspection, heartbeatState);
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
      const inspection = await this.inspectPlatformState(platformId, {
        cookies,
        savedState,
        probeCookieSignal: true,
      });
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
    if (this.auditDispatchInFlight) {
      return AUDIT_RUNNING;
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
    if (this.auditDispatchInFlight) {
      return AUDIT_RUNNING;
    }
    this.commentDispatchInFlight = this._dispatchPendingCommentSyncJobs();
    try {
      return await this.commentDispatchInFlight;
    } finally {
      this.commentDispatchInFlight = null;
    }
  }

  // One dispatcher at a time, in both directions: an audit round never starts while an
  // upload (ForWin or publish-hub) or a comment sync dispatch is running, and while it runs
  // those two skip (dispatchPendingUploadJobs / dispatchPendingCommentSyncJobs). The alarm
  // round runs the audit after them.
  async dispatchPendingAuditRequests() {
    if (this.auditDispatchInFlight) {
      // Not shared like the upload dispatch: callers (settings-updated, the next alarm)
      // must not wait for a round that can take minutes.
      return AUDIT_RUNNING;
    }
    if (this.dispatchInFlight || this.commentDispatchInFlight) {
      return { skipped: true, reason: 'busy' };
    }
    this.auditDispatchInFlight = this._dispatchPendingAuditRequests();
    try {
      return await this.auditDispatchInFlight;
    } finally {
      this.auditDispatchInFlight = null;
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

  // The heartbeat alarm's whole round: ForWin heartbeat, upload jobs, comment sync, then
  // audit requests. A publish-hub failure never stops it (the hub round records its own
  // errors).
  async heartbeatRound() {
    await this.heartbeatAndDispatchUploadJobs();
    return this._dispatchCommentSyncThenAuditRequests();
  }

  // Comment sync, then one round of audit requests (D8: audit sync is a direct ForWin
  // request channel, claimed like comment sync). Sequential, one job at a time. A comment
  // sync error does not keep the audit round from running, or a comment job that keeps
  // failing would starve every audit; the first error is raised once both ran.
  async _dispatchCommentSyncThenAuditRequests() {
    let commentResult;
    let firstError = null;
    try {
      commentResult = await this.dispatchPendingCommentSyncJobs();
    } catch (error) {
      firstError = error;
    }
    try {
      await this.dispatchPendingAuditRequests();
    } catch (error) {
      if (firstError) {
        await this._reportError('audit dispatch after a comment sync error', error);
      } else {
        firstError = error;
      }
    }
    if (firstError) {
      throw firstError;
    }
    return commentResult;
  }

  // With publish-hub configured, a ForWin heartbeat failure does not hold back
  // publish-hub jobs; the ForWin error is still raised, and a dispatch failure in
  // that round is reported, not dropped.
  async heartbeatAndDispatchUploadJobs() {
    try {
      await this.sendHeartbeat();
    } catch (error) {
      if (this._hubEnabled(await this.deps.getSettings())) {
        try {
          await this.dispatchPendingUploadJobs();
        } catch (dispatchError) {
          await this._reportError('upload dispatch after the ForWin heartbeat failed', dispatchError);
        }
      }
      throw error;
    }
    return this.dispatchPendingUploadJobs();
  }

  async _reportError(context, error) {
    if (typeof this.deps.reportError !== 'function') {
      return;
    }
    try {
      await this.deps.reportError(context, error);
    } catch (_error) {
      // Diagnostics must not change the round's outcome.
    }
  }

  // Records how the publish-hub part of a round went (background status and error
  // log). Hub errors are recorded here instead of raised, so an unreachable hub or a
  // refused key never stops ForWin's own work in the same round.
  async _recordHubRound(error) {
    if (typeof this.deps.recordHubRound !== 'function') {
      return;
    }
    try {
      await this.deps.recordHubRound(error ? { ok: false, error } : { ok: true });
    } catch (_error) {
      // Diagnostics must not change the round's outcome.
    }
  }

  async _dispatchPendingUploadJobs() {
    const settings = await this.deps.getSettings();
    const hub = this._hubEnabled(settings);
    if (hub) {
      return this._dispatchUploadJobsWithHub(settings, hub);
    }
    if (!settings.backendBaseUrl || !settings.apiKey) {
      return { skipped: true };
    }
    await this.syncUploadJournal();
    const clientId = await this.deps.getClientId();
    const connectedPlatforms = await this._collectConnectedPlatforms();
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
      if (!claimed?.found || !claimed.claim) {
        return handled ? { found: true, handled } : { found: false };
      }
      handled += 1;
      await this.executeUploadClaim(claimed.claim, 0);
    }
    return { found: true, handled, truncated: true };
  }

  // Both backends in one round, one job at a time (D7): publish-hub is asked first,
  // then ForWin, alternating until neither has work or eight jobs ran. A backend that
  // fails is left alone for the rest of the round so the other keeps working.
  // publish-hub errors are recorded (_recordHubRound), never raised, so the hub
  // being down (the VPS tunnel, a refused key) does not stop comment sync or the
  // heartbeat bookkeeping; a ForWin error is raised at the end, as before.
  // publish-hub's 409 attempt_in_progress only means "next round".
  async _dispatchUploadJobsWithHub(settings, hub) {
    let hubError = null;
    let forwinError = null;
    let hubOpen = true;
    try {
      await hub.sendExecutorHeartbeat();
      await hub.replayJournal();
    } catch (error) {
      hubOpen = false;
      hubError = error;
    }
    let forwinOpen = Boolean(settings.backendBaseUrl && settings.apiKey);
    if (forwinOpen) {
      try {
        await this.syncUploadJournal();
      } catch (error) {
        forwinOpen = false;
        forwinError = error;
      }
    }
    const connectedPlatforms = await this._collectConnectedPlatforms();
    const MAX_JOBS_PER_DISPATCH = 8;
    let handled = 0;
    let truncated = false;
    if (connectedPlatforms.length) {
      const forwinClientId = forwinOpen ? await this.deps.getClientId() : '';
      while (hubOpen || forwinOpen) {
        let progressed = false;
        if (hubOpen) {
          if (handled >= MAX_JOBS_PER_DISPATCH) {
            truncated = true;
            break;
          }
          try {
            const outcome = await hub.claimAndExecute(connectedPlatforms);
            if (outcome.claimed) {
              handled += 1;
              progressed = true;
            } else {
              hubOpen = false;
            }
          } catch (error) {
            hubOpen = false;
            hubError = hubError || error;
          }
        }
        if (forwinOpen) {
          if (handled >= MAX_JOBS_PER_DISPATCH) {
            truncated = true;
            break;
          }
          try {
            const claimed = await this.deps.backend.claimNextUploadJob({
              client_id: forwinClientId,
              connected_platforms: connectedPlatforms,
            });
            if (claimed?.found && claimed.claim) {
              handled += 1;
              progressed = true;
              await this.executeUploadClaim(claimed.claim, 0);
            } else {
              forwinOpen = false;
            }
          } catch (error) {
            forwinOpen = false;
            forwinError = forwinError || error;
          }
        }
        if (!progressed) {
          break;
        }
      }
    }
    await this._recordHubRound(hubError);
    if (forwinError) {
      throw forwinError;
    }
    if (!connectedPlatforms.length) {
      return { skipped: true };
    }
    if (!handled) {
      return { found: false };
    }
    return truncated ? { found: true, handled, truncated: true } : { found: true, handled };
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

  async _dispatchPendingAuditRequests() {
    const settings = await this.deps.getSettings();
    if (
      !settings.backendBaseUrl
      || !settings.apiKey
      || typeof this.deps.backend?.claimNextAuditRequest !== 'function'
    ) {
      return { skipped: true };
    }
    const clientId = await this.deps.getClientId();
    const connectedPlatforms = await this._collectConnectedPlatforms();
    if (!connectedPlatforms.length) {
      return { skipped: true };
    }
    // A platform that showed a risk page stays untouched for the rest of the round.
    const blockedPlatforms = new Set();
    let handled = 0;
    while (handled < MAX_AUDIT_REQUESTS_PER_DISPATCH) {
      const platforms = connectedPlatforms.filter((platformId) => !blockedPlatforms.has(platformId));
      if (!platforms.length) {
        break;
      }
      const request = await this.deps.backend.claimNextAuditRequest({
        client_id: clientId,
        connected_platforms: platforms,
      });
      if (!request?.request_id) {
        return handled ? { found: true, handled } : { found: false };
      }
      handled += 1;
      const outcome = await this.executeAuditRequest(request);
      if (outcome.riskBlocked) {
        blockedPlatforms.add(request.platform);
      }
    }
    return handled >= MAX_AUDIT_REQUESTS_PER_DISPATCH
      ? { found: true, handled, truncated: true }
      : { found: Boolean(handled), handled };
  }

  // Reads one book's audit state with the existing read-only audit page action and posts
  // {work, chapters, cover, milestones} to ForWin. Nothing is mutated and nothing goes to
  // publish-hub. When the page cannot be read (or shows a platform risk page) the failure and
  // its reason are posted instead: ForWin retries the request after its lease, gives it up at
  // the attempt cap, and fails it at once on a risk page.
  async executeAuditRequest(request) {
    const clientId = await this.deps.getClientId();
    const taskKey = `audit-request:${request.request_id}`;
    try {
      let platformResult;
      try {
        const adapter = getPlatformAdapter(request.platform);
        const targetUrl = String(
          request.upload_url
          || request.remote_url
          || adapter.dashboardUrl
          || adapter.publishUrl
          || '',
        );
        const tab = await this.deps.openUploadTab(targetUrl);
        this.registerExecutionTask(taskKey, tab.tabId);
        await this.waitForOpenedUploadTab(tab.tabId, request.platform, 8000);
        platformResult = await this.deps.runAuditSyncCommand(tab.tabId, {
          platform: request.platform,
          task_kind: 'audit_sync',
          remote_book_id: String(request.remote_book_id || ''),
          upload_url: request.upload_url || '',
          remote_url: request.remote_url || '',
        });
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error);
        await this._reportError(`audit request ${request.request_id} page read failed`, error);
        await this._postAuditFailure(request, clientId, { reason });
        return { status: 'failed' };
      }
      if (this._isRiskPauseSignal(platformResult)) {
        const rawRiskReason = String(platformResult?.riskReason || platformResult?.risk_reason || '');
        const reason = String(platformResult?.error || platformResult?.errorMessage || '')
          || `Publisher ${rawRiskReason || 'risk'} risk signal detected.`;
        await this._reportError(
          `audit request ${request.request_id} stopped at a platform risk page`,
          new Error(reason),
        );
        await this._postAuditFailure(request, clientId, {
          reason,
          // only the reasons ForWin knows; anything else is a plain failure
          riskReason: isRiskReason(rawRiskReason) ? rawRiskReason : '',
          currentUrl: String(platformResult?.currentUrl || platformResult?.current_url || ''),
        });
        return { status: 'risk', riskBlocked: true };
      }
      const observed = buildAttemptResult({
        job: { task_kind: 'audit_sync' },
        result: platformResult,
      });
      if (observed.outcome !== 'succeeded') {
        const reason = observed.error_message || observed.error_code || 'audit page read failed';
        await this._reportError(
          `audit request ${request.request_id} was not read`,
          new Error(reason),
        );
        await this._postAuditFailure(request, clientId, {
          reason,
          currentUrl: observed.current_url,
        });
        return { status: 'failed' };
      }
      try {
        await this.deps.backend.submitAuditRequestResult(request.request_id, {
          client_id: clientId,
          ...observed.details,
          current_url: observed.current_url,
        });
      } catch (error) {
        // 404 / 409 / 422: ForWin no longer wants this answer (finished, taken over by
        // another client after the lease, or not the book's page, which it records as a
        // failure itself). Nothing to retry.
        if ([404, 409, 422].includes(error?.status)) {
          await this._reportError(`audit request ${request.request_id} result refused`, error);
          return { status: 'refused', code: String(error.code || '') };
        }
        throw error;
      }
      return { status: 'succeeded' };
    } finally {
      await this.cleanupExecutionTabs(taskKey).catch(() => ({ attempted: false }));
    }
  }

  // Tells ForWin why a request could not be read. A delivery problem is only reported: the
  // request stays claimed and comes back after its lease, so nothing is lost.
  async _postAuditFailure(request, clientId, { reason, riskReason = '', currentUrl = '' }) {
    if (typeof this.deps.backend?.submitAuditRequestFailure !== 'function') {
      return;
    }
    const body = {
      client_id: clientId,
      reason: String(reason || 'audit page could not be read').slice(0, 2000),
    };
    if (riskReason) {
      body.risk_reason = riskReason;
    }
    if (currentUrl) {
      body.current_url = String(currentUrl).slice(0, 2000);
    }
    try {
      await this.deps.backend.submitAuditRequestFailure(request.request_id, body);
    } catch (error) {
      await this._reportError(`audit request ${request.request_id} failure not delivered`, error);
    }
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
      if (inspection.loginVisible) {
        await this.maybeNotifyLoginQr(session, inspection);
      }
      await this.deps.notifyPage(session.originTabId, 'login-status', {
        platform: session.platformId,
        connected: false,
        message: inspection.loginVisible
          ? '已打开登录页，请继续扫码或完成登录。'
          : '正在等待平台确认作者后台登录状态...',
      });
    }
  }

  async maybeNotifyLoginQr(session, inspection) {
    const nowMs = typeof this.deps.nowMs === 'function' ? Number(this.deps.nowMs()) : defaultNowMs();
    const currentUrl = String(inspection?.currentUrl || session.lastUrl || '');
    if (await this.areLoginQrNotificationsDisabled(session)) {
      return { skipped: true, reason: 'notifications-disabled' };
    }
    const lastNotifiedAtMs = await this.getLoginQrLastNotifiedAtMs(session, currentUrl);
    if (session.loginQrNotificationInFlight) {
      return { skipped: true, reason: 'in-flight' };
    }
    if (lastNotifiedAtMs > 0 && nowMs - lastNotifiedAtMs < LOGIN_QR_NOTIFICATION_THROTTLE_MS) {
      return { skipped: true, reason: 'throttled' };
    }
    if (
      !inspection?.loginVisible
      || typeof this.deps.captureLoginQrImage !== 'function'
      || typeof this.deps.backend?.notifyLoginQr !== 'function'
    ) {
      return { skipped: true };
    }
    session.loginQrNotificationInFlight = true;
    try {
      const settings = await this.deps.getSettings();
      if (!loginQrNotificationsAllowed(settings, nowMs)) {
        await this.recordLoginQrNotificationEvent({
          platform: session.platformId,
          tab_id: session.popupTabId,
          current_url: currentUrl,
          phase: 'skipped',
          reason: 'login-qr-notifications-disabled-by-settings',
        });
        return { skipped: true, reason: 'login-qr-notifications-disabled-by-settings' };
      }
      if (!settings.backendBaseUrl || !settings.apiKey) {
        await this.recordLoginQrNotificationEvent({
          platform: session.platformId,
          tab_id: session.popupTabId,
          current_url: String(inspection.currentUrl || session.lastUrl || ''),
          phase: 'skipped',
          reason: 'backend-settings-missing',
        });
        return { skipped: true };
      }
      await this.recordLoginQrNotificationEvent({
        platform: session.platformId,
        tab_id: session.popupTabId,
        current_url: currentUrl,
        phase: 'capture-start',
      });
      const capture = await this.deps.captureLoginQrImage(
        session.popupTabId,
        session.platformId,
        inspection,
      );
      const imageDataUrl = typeof capture === 'string'
        ? capture
        : String(capture?.imageDataUrl || capture?.image_data_url || '');
      const captureSource = String(capture?.source || '');
      if (!imageDataUrl) {
        await this.recordLoginQrNotificationEvent({
          platform: session.platformId,
          tab_id: session.popupTabId,
          current_url: currentUrl,
          phase: 'capture-empty',
          reason: String(capture?.error || 'login-qr-image-empty'),
          source: captureSource,
        });
        return { skipped: true };
      }
      if (captureSource === 'debugger-screenshot') {
        await this.recordLoginQrNotificationEvent({
          platform: session.platformId,
          tab_id: session.popupTabId,
          current_url: currentUrl,
          phase: 'capture-rejected',
          reason: 'non-qr-screenshot-capture',
          source: captureSource,
          image_data_url_length: imageDataUrl.length,
        });
        return { skipped: true };
      }
      await this.recordLoginQrNotificationEvent({
        platform: session.platformId,
        tab_id: session.popupTabId,
        current_url: currentUrl,
        phase: 'captured',
        source: captureSource,
        image_data_url_length: imageDataUrl.length,
      });
      const result = await this.deps.backend.notifyLoginQr({
        client_id: await this.deps.getClientId(),
        platform: session.platformId,
        current_url: currentUrl,
        image_data_url: imageDataUrl,
        source: captureSource,
        captured_at: new Date().toISOString(),
      });
      if (result?.ok) {
        await this.setLoginQrLastNotifiedAtMs(session, currentUrl, nowMs);
        await this.setLoginQrLastNotifiedAtMs(session, LOGIN_QR_PLATFORM_THROTTLE_URL, nowMs);
      }
      if (loginQrNotificationsDisabled(result)) {
        await this.setLoginQrNotificationsDisabled(session, true);
      }
      await this.recordLoginQrNotificationEvent({
        platform: session.platformId,
        tab_id: session.popupTabId,
        current_url: currentUrl,
        phase: 'sent',
        ok: Boolean(result?.ok),
        dispatched: Boolean(result?.dispatched),
        message: String(result?.message || ''),
      });
      return result;
    } catch (error) {
      await this.recordLoginQrNotificationEvent({
        platform: session.platformId,
        tab_id: session.popupTabId,
        current_url: String(inspection.currentUrl || session.lastUrl || ''),
        phase: 'failed',
        error: error instanceof Error ? error.message : String(error || ''),
      });
      return { ok: false };
    } finally {
      session.loginQrNotificationInFlight = false;
    }
  }

  async recordLoginQrNotificationEvent(event) {
    if (typeof this.deps.recordLoginQrNotification !== 'function') {
      return;
    }
    try {
      await this.deps.recordLoginQrNotification({
        ...(event || {}),
        at: new Date().toISOString(),
      });
    } catch (_error) {
      // Diagnostic storage must not block login checks or publisher jobs.
    }
  }

  async getLoginQrLastNotifiedAtMs(session, currentUrl) {
    const memoryValue = Number(session?.loginQrLastNotifiedAtMs || 0);
    if (typeof this.deps.getLoginQrLastNotifiedAtMs !== 'function') {
      return memoryValue;
    }
    try {
      const storedValue = Number(
        await this.deps.getLoginQrLastNotifiedAtMs(session.platformId, currentUrl),
      );
      const platformValue = Number(
        await this.deps.getLoginQrLastNotifiedAtMs(
          session.platformId,
          LOGIN_QR_PLATFORM_THROTTLE_URL,
        ),
      );
      return Math.max(
        memoryValue,
        Number.isFinite(storedValue) ? storedValue : 0,
        Number.isFinite(platformValue) ? platformValue : 0,
      );
    } catch (_error) {
      return memoryValue;
    }
  }

  async setLoginQrLastNotifiedAtMs(session, currentUrl, notifiedAtMs) {
    const value = Number(notifiedAtMs || 0);
    session.loginQrLastNotifiedAtMs = value;
    if (typeof this.deps.setLoginQrLastNotifiedAtMs !== 'function') {
      return;
    }
    try {
      await this.deps.setLoginQrLastNotifiedAtMs(session.platformId, currentUrl, value);
    } catch (_error) {
      // Persistent throttle state is best-effort; in-memory throttling still applies.
    }
  }

  async areLoginQrNotificationsDisabled(session) {
    if (session?.loginQrNotificationsDisabled) {
      return true;
    }
    if (typeof this.deps.getLoginQrNotificationsDisabled !== 'function') {
      return false;
    }
    try {
      const disabled = Boolean(await this.deps.getLoginQrNotificationsDisabled(session.platformId));
      if (disabled) {
        session.loginQrNotificationsDisabled = true;
      }
      return disabled;
    } catch (_error) {
      return false;
    }
  }

  async setLoginQrNotificationsDisabled(session, disabled) {
    session.loginQrNotificationsDisabled = Boolean(disabled);
    if (typeof this.deps.setLoginQrNotificationsDisabled !== 'function') {
      return;
    }
    try {
      await this.deps.setLoginQrNotificationsDisabled(session.platformId, Boolean(disabled));
    } catch (_error) {
      // Persistent disabled state is best-effort; in-memory disabled state still applies.
    }
  }

  async recordHeartbeatPlatformState(event) {
    if (typeof this.deps.recordHeartbeatPlatformState !== 'function') {
      return;
    }
    try {
      await this.deps.recordHeartbeatPlatformState({
        ...(event || {}),
        at: new Date().toISOString(),
      });
    } catch (_error) {
      // Diagnostic storage must not block heartbeat delivery.
    }
  }

  clearHeartbeatLoginQrNotifications(platformId) {
    const prefix = `${platformId}:`;
    for (const key of Array.from(this.heartbeatLoginQrNotificationSessions.keys())) {
      if (key.startsWith(prefix)) {
        this.heartbeatLoginQrNotificationSessions.delete(key);
      }
    }
  }

  async maybeNotifyHeartbeatLoginQr(platformId, inspection, heartbeatState) {
    const rawState = heartbeatState?.raw_state || {};
    const loginVisible = rawState.page_login_visible && !rawState.page_authenticated;
    if (!loginVisible) {
      this.clearHeartbeatLoginQrNotifications(platformId);
      return { skipped: true };
    }
    const tabId = Number(inspection?.tabId || inspection?.tab_id || 0);
    if (!tabId) {
      return { skipped: true };
    }
    const currentUrl = String(inspection?.currentUrl || inspection?.url || rawState.current_url || '');
    const activeSession = this.loginSessions.get(tabId);
    if (activeSession?.platformId === platformId) {
      activeSession.lastUrl = currentUrl || activeSession.lastUrl;
      await this.recordLoginQrNotificationEvent({
        platform: platformId,
        tab_id: tabId,
        current_url: currentUrl,
        phase: 'skipped',
        reason: 'heartbeat-active-login-session-qr-suppressed',
      });
      return { skipped: true, reason: 'heartbeat-active-login-session-qr-suppressed' };
    }
    this.heartbeatLoginQrNotificationSessions.delete(`${platformId}:${tabId}`);
    await this.recordLoginQrNotificationEvent({
      platform: platformId,
      tab_id: tabId,
      current_url: currentUrl,
      phase: 'skipped',
      reason: 'heartbeat-login-page-without-active-login-session',
    });
    return { skipped: true, reason: 'heartbeat-login-page-without-active-login-session' };
  }
}
