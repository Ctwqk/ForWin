import { normalizeSettings } from './settings.js';

async function parseJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `HTTP ${response.status}`);
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

  return {
    async heartbeat(payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/extension/heartbeat`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(payload),
      });
      return parseJson(response);
    },

    async getUploadJob(jobId) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/upload-jobs/${jobId}`);
      return parseJson(response);
    },

    async updateUploadJobResult(jobId, payload) {
      const response = await fetchImpl(`${settings.backendBaseUrl}/api/publishers/upload-jobs/${jobId}/result`, {
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
