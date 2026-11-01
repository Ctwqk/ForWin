import { normalizeSettings } from './settings.js';

async function parseJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.detail;
    const detailMessage = typeof detail === 'string' ? detail : detail?.message;
    const error = new Error(
      detailMessage || payload?.message || payload?.error?.message || `HTTP ${response.status}`,
    );
    error.status = response.status;
    error.code = payload?.error?.code ?? payload?.code ?? detail?.code;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export function createBackendClient(fetchImpl, rawSettings) {
  const settings = normalizeSettings(rawSettings);

  function headers() {
    return {
      'Content-Type': 'application/json',
      'X-Forwin-Extension-Key': settings.apiKey,
    };
  }

  async function postUploadAttempt(jobId, attemptId, action, payload) {
    const encodedJobId = encodeURIComponent(jobId);
    const encodedAttemptId = encodeURIComponent(attemptId);
    const response = await fetchImpl(
      `${settings.backendBaseUrl}/api/publishers/extension/upload-jobs/${encodedJobId}/attempts/${encodedAttemptId}/${action}`,
      {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      },
    );
    return parseJson(response);
  }

  return {
    async heartbeat(payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/extension/heartbeat`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      });
      return parseJson(response);
    },

    async claimNextUploadJob(payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/extension/upload-jobs/claim`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      });
      return parseJson(response);
    },

    async heartbeatUploadAttempt(jobId, attemptId, payload) {
      return postUploadAttempt(jobId, attemptId, 'heartbeat', payload);
    },

    async updateUploadAttemptPhase(jobId, attemptId, payload) {
      return postUploadAttempt(jobId, attemptId, 'phase', payload);
    },

    async pauseUploadAttempt(jobId, attemptId, payload) {
      return postUploadAttempt(jobId, attemptId, 'pause', payload);
    },

    async submitUploadAttemptResult(jobId, attemptId, payload) {
      return postUploadAttempt(jobId, attemptId, 'result', payload);
    },

    async submitUploadReceipt(jobId, attemptId, payload) {
      return postUploadAttempt(jobId, attemptId, 'receipt', payload);
    },

    async reconcileUploadAttempt(jobId, attemptId, payload) {
      return postUploadAttempt(jobId, attemptId, 'reconcile', payload);
    },

    async claimNextCommentSyncJob(payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/extension/comment-sync-jobs/claim`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      });
      return parseJson(response);
    },

    async syncBrowserSession(payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/extension/session-sync`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      });
      return parseJson(response);
    },

    async notifyLoginQr(payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/extension/login-qr`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      });
      return parseJson(response);
    },

    async getBrowserSession(platformId) {
      const response = await fetchImpl(
        `${settings.backendBaseUrl}/api/publishers/extension/browser-sessions/${encodeURIComponent(platformId)}`,
        {
          headers: headers(),
        },
      );
      return parseJson(response);
    },

    async syncCommentsBatch(payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/extension/comments/batch`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      });
      return parseJson(response);
    },

    async updateCommentSyncJobResult(jobId, payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/comment-sync-jobs/${jobId}/result`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      });
      return parseJson(response);
    },
  };
}

const HUB_REQUEST_TIMEOUT_MS = 30000;

// publish-hub's executor API (spec 7.2). Authenticated with the hub's own key as a
// bearer token; the ForWin key is never sent here and the hub key never to ForWin.
// Errors carry `status` and the envelope's `code` ({"error": {"code", "message"}}).
export function createHubClient(fetchImpl, rawSettings, options = {}) {
  const settings = normalizeSettings(rawSettings);
  const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : HUB_REQUEST_TIMEOUT_MS;

  async function post(path, payload) {
    const abort = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = abort ? globalThis.setTimeout(() => abort.abort(), timeoutMs) : null;
    timer?.unref?.();
    try {
      const response = await fetchImpl(`${settings.hubBaseUrl}${path}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${settings.hubApiKey}`,
        },
        body: JSON.stringify(payload),
        ...(abort ? { signal: abort.signal } : {}),
      });
      if (response.status === 204) {
        return null;
      }
      return await parseJson(response);
    } finally {
      if (timer) {
        globalThis.clearTimeout(timer);
      }
    }
  }

  return {
    async heartbeat(payload) {
      return post('/v1/executor/heartbeat', payload);
    },

    // null when there is nothing to claim (204).
    async claim(payload) {
      return post('/v1/executor/upload-jobs/claim', payload);
    },

    // action: heartbeat | phase | receipt | result | pause | reconcile
    async attempt(jobId, attemptId, action, payload) {
      const encodedJobId = encodeURIComponent(jobId);
      const encodedAttemptId = encodeURIComponent(attemptId);
      return post(
        `/v1/executor/upload-jobs/${encodedJobId}/attempts/${encodedAttemptId}/${action}`,
        payload,
      );
    },
  };
}
