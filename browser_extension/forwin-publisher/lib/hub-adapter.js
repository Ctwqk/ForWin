// publish-hub adapter: claims qidian/fanqie jobs from publish-hub's executor API,
// translates them into the extension's internal claim shape, drives the existing
// page automation and reports back under the executor contract (roadmap 3.1):
//
// - one job at a time (the controller's dispatch runs jobs one after another);
// - an attempt heartbeat every 30 s while working;
// - phase mutation_started is reported, and acknowledged, before any action that
//   can change the platform (run-upload, a cover upload, run-create-book);
// - after mutation_started, permanent_error only on an explicit platform refusal,
//   anything else is retryable_error (publish-hub then reconciles);
// - stale_epoch, lease_expired, job_cancelled (or the hub's not_found) end the job
//   at once: no further platform action and no further report for it;
// - a receipt refused with remote_id_conflict pauses the job with needs_human;
// - final reports are journaled before they are sent and resent unchanged.
import { getPlatformAdapter } from './platforms.js?v=0.1.66';
import { buildExecutionReceipt, buildReconciliationRequest } from './reconciliation.js?v=0.1.66';
import { guardRiskInspection } from './risk-inspection.js?v=0.1.66';
import { isHubConfigured } from './settings.js';

export const HUB_BACKEND = 'hub';
export const HUB_PLATFORMS = Object.freeze(['qidian', 'fanqie']);
export const HUB_DEFER_MS = 5 * 60_000;
export const HUB_HEARTBEAT_INTERVAL_MS = 30_000;

const HEARTBEAT_INTERVAL_SECONDS = HUB_HEARTBEAT_INTERVAL_MS / 1000;
const MAX_ERROR_BYTES = 16 * 1024;
const MAX_RAW_BYTES = 64 * 1024;
const MAX_TEXT = 2000;
const MAX_REMOTE_URL = 2048;
const MAX_REMOTE_ID = 512;
const MAX_CANDIDATE_TEXT = 160;
const FENCE_CODES = new Set(['stale_epoch', 'lease_expired', 'job_cancelled', 'not_found']);
const TAG_GROUPS = ['theme', 'role', 'plot'];
const CREATE_URLS = {
  fanqie: 'https://fanqienovel.com/main/writer/create',
  qidian: 'https://write.qq.com/portal/dashboard/create-novel?from=S5',
};
const CREATE_PAGE_MARKERS = {
  fanqie: '/main/writer/create',
  qidian: '/create-novel',
};
const PLATFORM_HOSTS = {
  fanqie: ['fanqienovel.com'],
  qidian: ['write.qq.com', 'pcwrite.yuewen.com'],
};
const CREATE_PLATFORM_REFUSALS = new Set(['create-book-rate-limited', 'create-book-validation-failed']);

function text(value) {
  return String(value ?? '').trim();
}

function first(...values) {
  return values.map(text).find(Boolean) || '';
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;
const MAX_JSON_DEPTH = 32;

// Text publish-hub can store (its is_storable_text refuses NUL and unpaired
// surrogates, and a refused report would be resent unchanged until the lease
// runs out): both become U+FFFD. With `max`, cut to at most `max` code points
// (the hub counts code points), never inside a surrogate pair.
export function storableText(value, max = 0) {
  const clean = String(value ?? '').replace(LONE_SURROGATE, '\uFFFD').replace(/\u0000/g, '\uFFFD');
  if (!(max > 0)) {
    return clean;
  }
  const points = Array.from(clean);
  return points.length > max ? `${points.slice(0, max - 1).join('')}…` : clean;
}

// A JSON value publish-hub can store: every string and key through storableText,
// non-finite numbers as null (what JSON.stringify sends anyway), undefined and
// functions left out, nesting deeper than MAX_JSON_DEPTH replaced by a marker.
export function storableJson(value, depth = 0) {
  if (typeof value === 'string') {
    return storableText(value);
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }
  if (value === null || typeof value === 'boolean') {
    return value;
  }
  if (typeof value !== 'object') {
    return undefined;
  }
  if (depth >= MAX_JSON_DEPTH) {
    return '[depth limit]';
  }
  if (Array.isArray(value)) {
    return value.map((item) => {
      const stored = storableJson(item, depth + 1);
      return stored === undefined ? null : stored;
    });
  }
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    const stored = storableJson(item, depth + 1);
    if (stored !== undefined) {
      result[storableText(key)] = stored;
    }
  }
  return result;
}

function truncate(value, max = MAX_TEXT) {
  return storableText(value, max);
}

function codePointLength(value) {
  return Array.from(String(value ?? '')).length;
}

function jsonBytes(value) {
  return new TextEncoder().encode(JSON.stringify(value)).length;
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error ?? '');
}

export function isFencedError(error) {
  return FENCE_CODES.has(String(error?.code || ''));
}

function resultPayloadOf(result) {
  const payload = result?.resultPayload || result?.result_payload;
  return isPlainObject(payload) ? payload : {};
}

async function sha256Hex(value) {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(String(value)));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

// --- translation ---------------------------------------------------------------------------

export function hubTaskKind(action, contentKind) {
  const hubAction = text(action);
  if (hubAction === 'upload') {
    return text(contentKind) === 'novel_chapter' ? 'chapter_upload' : '';
  }
  if (hubAction === 'create_destination') {
    return 'create_book';
  }
  if (hubAction === 'upload_cover') {
    return 'cover_upload';
  }
  return '';
}

// The hub's create_destination meta {audience, category, tags[], protagonist_names[],
// intro} back in the shape createFanqieBook/createQidianBook take (ruling R22): a tag
// "<group>:<tag>" with group theme|role|plot (split on the FIRST ":") goes to that
// group, any other entry stays a plain tag. Empty values are left out, so an empty
// meta decodes to {}.
export function decodeHubBookMeta(bookMeta) {
  if (!isPlainObject(bookMeta)) {
    return {};
  }
  const decoded = {};
  const audience = text(bookMeta.audience);
  if (audience) {
    decoded.audience = audience;
  }
  const category = text(bookMeta.category);
  if (category) {
    decoded.primary_category = category;
  }
  const flat = [];
  for (const entry of Array.isArray(bookMeta.tags) ? bookMeta.tags : []) {
    const raw = String(entry ?? '');
    const separator = raw.indexOf(':');
    const group = separator > 0 ? raw.slice(0, separator) : '';
    if (TAG_GROUPS.includes(group)) {
      const tag = raw.slice(separator + 1).trim();
      if (tag) {
        const key = `${group}_tags`;
        decoded[key] = [...(decoded[key] || []), tag];
      }
    } else if (raw.trim()) {
      flat.push(raw.trim());
    }
  }
  if (flat.length) {
    decoded.tags = flat;
  }
  const names = (Array.isArray(bookMeta.protagonist_names) ? bookMeta.protagonist_names : [])
    .map(text)
    .filter(Boolean);
  if (names.length) {
    decoded.protagonist_names = names;
  }
  const intro = text(bookMeta.intro);
  if (intro) {
    decoded.intro = intro;
  }
  return decoded;
}

// ForWin sends "file://" + the percent-encoded container path; DOM.setFileInputFiles
// needs the plain path. '' for anything that is not a local absolute file path.
export function decodeFileUri(uri) {
  const value = text(uri);
  if (!value.startsWith('file:///')) {
    return '';
  }
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== 'file:' || parsed.hostname || parsed.search || parsed.hash) {
      return '';
    }
    const path = decodeURIComponent(parsed.pathname);
    if (!path.startsWith('/') || path.includes('\0')) {
      return '';
    }
    return path;
  } catch (_error) {
    return '';
  }
}

export function hubPauseReason(riskReason) {
  const reason = text(riskReason);
  return reason === 'captcha' || reason === 'mfa' ? reason : 'risk';
}

function coverRef(contentRefs) {
  return (Array.isArray(contentRefs) ? contentRefs : [])
    .find((ref) => isPlainObject(ref) && text(ref.role) === 'cover') || null;
}

function onPlatformHost(platform, url) {
  try {
    const parsed = new URL(text(url));
    const host = parsed.hostname.toLowerCase();
    return parsed.protocol === 'https:'
      && (PLATFORM_HOSTS[platform] || []).some((allowed) => host === allowed || host.endsWith(`.${allowed}`));
  } catch (_error) {
    return false;
  }
}

function hubInput(taskKind, { platform, payload, destination, settings, cover }) {
  if (taskKind === 'chapter_upload') {
    // The legacy chapter input. No remote_book_id: the receipt must take the book
    // id from what the page showed, so a chapter saved into another book is seen.
    return {
      book_name: text(payload.book_name),
      chapter_title: text(payload.chapter_title),
      body: String(payload.body_text ?? ''),
      publish: text(payload.mode) === 'publish',
      create_if_missing: false,
    };
  }
  if (taskKind === 'cover_upload') {
    const bookUrl = text(destination.remote_url);
    return {
      book_name: text(settings.book_name),
      remote_book_id: text(destination.remote_id),
      remote_url: bookUrl,
      upload_url: bookUrl,
      file_path: decodeFileUri(cover?.uri),
    };
  }
  if (taskKind === 'create_book') {
    const coverPath = decodeFileUri(cover?.uri);
    return {
      platform,
      book_name: text(settings.book_name),
      book_meta: decodeHubBookMeta(payload.book_meta),
      ...(coverPath ? { cover_path: coverPath } : {}),
    };
  }
  return {};
}

async function contentSha256(taskKind, { platform, payload, input, cover }) {
  if (taskKind === 'chapter_upload') {
    return sha256Hex(String(payload.body_text ?? ''));
  }
  if (taskKind === 'cover_upload') {
    const declared = text(cover?.sha256).toLowerCase();
    return /^[0-9a-f]{64}$/.test(declared) ? declared : sha256Hex(text(cover?.uri));
  }
  return sha256Hex(JSON.stringify({ platform, input }));
}

// publish-hub claim -> the extension's internal claim:
//   mode -> execution_mode; action + content_kind -> task_kind; payload, destination
//   and content_refs -> input; epoch -> lease_epoch; attempt_id -> attempt_id;
//   content_sha256 computed here from the chapter body.
// Only stable fields are kept, so a claim the hub hands out again translates the same.
export async function translateHubClaim(raw) {
  const job = isPlainObject(raw?.job) ? raw.job : {};
  const jobId = text(job.id);
  const attemptId = text(raw?.attempt_id);
  if (!jobId || !attemptId) {
    throw new Error('publish-hub claim is missing its job id or attempt id.');
  }
  const mode = text(raw?.mode);
  if (mode !== 'execute' && mode !== 'reconcile') {
    throw new Error(`publish-hub claim has an unsupported attempt mode: ${mode || '(none)'}`);
  }
  const epoch = Number(raw?.epoch);
  if (!Number.isInteger(epoch) || epoch < 1) {
    throw new Error('publish-hub claim has no valid epoch.');
  }
  const destination = isPlainObject(job.destination) ? job.destination : {};
  const settings = isPlainObject(destination.settings) ? destination.settings : {};
  const payload = isPlainObject(job.payload) ? job.payload : {};
  const platform = text(job.account?.platform);
  const taskKind = hubTaskKind(job.action, job.content_kind);
  const cover = coverRef(job.content_refs);
  const input = hubInput(taskKind, { platform, payload, destination, settings, cover });
  const uploadUrl = text(settings.upload_url);
  return {
    execution_mode: mode,
    job: {
      job_id: jobId,
      task_kind: taskKind,
      platform,
      content_sha256: await contentSha256(taskKind, { platform, payload, input, cover }),
      input,
      hub: {
        action: text(job.action),
        content_kind: text(job.content_kind),
        source_ref: text(job.source_ref),
        destination_id: text(destination.id || job.destination_id),
        destination_remote_id: text(destination.remote_id),
        destination_remote_url: text(destination.remote_url),
        destination_book_name: text(settings.book_name),
        upload_url: onPlatformHost(platform, uploadUrl) ? uploadUrl : '',
        cover_uri: text(cover?.uri),
      },
    },
    attempt: {
      attempt_id: attemptId,
      lease_epoch: epoch,
      phase: 'claimed',
      lease_expires_at: text(raw.lease_expires_at),
      heartbeat_interval_seconds: HEARTBEAT_INTERVAL_SECONDS,
    },
  };
}

// --- checks before anything touches the platform -------------------------------------------

function problem(code, message) {
  return { outcome: 'permanent_error', error: { code, message } };
}

// What must stop an execute attempt before any platform action, or null.
export function hubPreflight(job) {
  if (!HUB_PLATFORMS.includes(job.platform)) {
    return problem('unsupported_platform', `publish-hub job for unsupported platform ${job.platform || '(none)'}.`);
  }
  if (!job.task_kind) {
    return problem(
      'unsupported_job',
      `This executor does not run ${job.hub.action || '(none)'}/${job.hub.content_kind || '(none)'} jobs.`,
    );
  }
  const input = job.input;
  if (job.task_kind === 'chapter_upload' || job.task_kind === 'cover_upload') {
    if (!job.hub.destination_remote_id) {
      // D4: the book does not exist yet; its create job must succeed first.
      return { outcome: 'defer', error: { code: 'destination_has_no_remote_id', message: 'The book has no platform id yet.' } };
    }
  }
  if (job.task_kind === 'chapter_upload') {
    if (!input.book_name || !input.chapter_title || !input.body.trim()) {
      return problem('invalid_payload', 'The chapter job is missing its book name, title or body.');
    }
    const destinationName = job.hub.destination_book_name;
    if (destinationName && destinationName !== input.book_name) {
      return problem(
        'book_name_mismatch',
        `The chapter names book "${input.book_name}" but the destination is "${destinationName}".`,
      );
    }
  }
  if (job.task_kind === 'cover_upload' && !input.file_path) {
    return problem('cover_file_uri_invalid', `The cover content ref is not a local file uri: ${truncate(job.hub.cover_uri, 500)}`);
  }
  if (job.task_kind === 'create_book') {
    if (job.hub.destination_remote_id) {
      return problem('destination_already_created', `The destination already has book ${job.hub.destination_remote_id}.`);
    }
    if (!input.book_name) {
      return problem('book_name_missing', 'destination.settings.book_name is empty.');
    }
    // Never let the page fall back to invented defaults (a made-up category or
    // intro) for a book that cannot be deleted afterwards.
    const missing = ['primary_category', 'intro'].filter((key) => !text(input.book_meta?.[key]));
    if (missing.length) {
      return problem('book_meta_unusable', `book_meta has no ${missing.join(' or ')}.`);
    }
  }
  return null;
}

function hubTargetUrl(job) {
  const adapter = getPlatformAdapter(job.platform);
  if (job.task_kind === 'create_book') {
    return CREATE_URLS[job.platform];
  }
  if (job.task_kind === 'cover_upload') {
    const bookUrl = job.hub.destination_remote_url;
    return onPlatformHost(job.platform, bookUrl) ? bookUrl : adapter.dashboardUrl;
  }
  return job.hub.upload_url || adapter.dashboardUrl;
}

// --- report bodies -----------------------------------------------------------------------

function fence(entry) {
  return {
    client_id: entry.client_id,
    epoch: Number(entry.attempt.lease_epoch),
  };
}

function boundedError(error) {
  const bounded = storableJson({
    code: truncate(first(error?.code, 'extension-execution-failed'), 200),
    message: truncate(error?.message ?? ''),
    ...(text(error?.current_url) ? { current_url: truncate(error.current_url) } : {}),
    ...(isPlainObject(error?.details) ? { details: error.details } : {}),
  });
  if (jsonBytes(bounded) > MAX_ERROR_BYTES) {
    delete bounded.details;
    bounded.details_truncated = true;
  }
  return bounded;
}

function platformError(result, fallbackCode) {
  const payload = resultPayloadOf(result);
  return {
    code: first(result?.errorCode, result?.error_code, fallbackCode),
    message: first(result?.error, result?.errorMessage, result?.error_message, result?.message),
    current_url: first(result?.currentUrl, result?.current_url),
    details: Object.keys(payload).length ? payload : undefined,
  };
}

function boundedRaw(raw) {
  const stored = storableJson(raw);
  if (jsonBytes(stored) <= MAX_RAW_BYTES) {
    return stored;
  }
  const { evidence: _evidence, ...identity } = stored;
  return { ...identity, evidence_truncated: true };
}

// Every report body is built here and passed through storableJson last, so no
// page text can make publish-hub refuse it.
function resultRecord(entry, outcome, extras = {}) {
  return {
    report: 'result',
    outcome,
    body: storableJson({
      ...fence(entry),
      outcome,
      ...(extras.error ? { error: boundedError(extras.error) } : {}),
      ...(extras.defer_until ? { defer_until: extras.defer_until } : {}),
    }),
  };
}

function reconcileRecord(entry, finding) {
  return { report: 'reconcile', outcome: finding, body: storableJson({ ...fence(entry), finding }) };
}

function pauseRecord(entry, reason, detail) {
  return { body: storableJson({ ...fence(entry), reason, detail: truncate(detail) }) };
}

function receiptRecord(entry, receipt) {
  const remoteId = storableText(text(receipt.remote_id));
  const remoteUrl = storableText(text(receipt.remote_url));
  return {
    body: storableJson({
      ...fence(entry),
      official_state: truncate(receipt.official_state, 64),
      ...(remoteId && codePointLength(remoteId) <= MAX_REMOTE_ID ? { remote_id: remoteId } : {}),
      ...(remoteUrl && codePointLength(remoteUrl) <= MAX_REMOTE_URL ? { remote_url: remoteUrl } : {}),
      raw: boundedRaw(receipt.raw || {}),
    }),
  };
}

// The legacy receipt (lib/reconciliation.js) as a hub receipt: chapters report the
// chapter id as remote_id, covers have no remote id, the evidence goes into raw.
function hubReceiptFromLegacy(job, legacy) {
  const raw = {
    remote_book_id: legacy.remote_book_id,
    remote_chapter_id: legacy.remote_chapter_id,
    content_sha256: legacy.content_sha256,
    observed_at: legacy.observed_at,
    evidence: legacy.evidence,
  };
  return {
    official_state: legacy.official_state,
    remote_id: job.task_kind === 'chapter_upload' ? legacy.remote_chapter_id : '',
    remote_url: legacy.remote_url,
    raw,
  };
}

function createReceipt(remoteBookId, remoteUrl, evidence, observedAt) {
  return {
    official_state: 'created',
    remote_id: remoteBookId,
    remote_url: remoteUrl,
    raw: { remote_book_id: remoteBookId, observed_at: observedAt, ...evidence },
  };
}

// A risk pause reply, or a failed reply that saw a risk signal after the action
// (run-create-book's unknown outcome carries resultPayload.risk_reason).
function riskReasonOf(result) {
  if (result?.riskPause || result?.risk_pause) {
    return first(result.riskReason, result.risk_reason) || 'risk';
  }
  if (result?.ok) {
    return null;
  }
  return text(resultPayloadOf(result).risk_reason) || null;
}

function riskDetail(result) {
  const evidence = isPlainObject(result?.riskEvidence)
    ? result.riskEvidence
    : isPlainObject(result?.risk_evidence) ? result.risk_evidence : {};
  const payload = resultPayloadOf(result);
  return first(
    evidence.message,
    evidence.matchedText,
    evidence.matched_text,
    result?.platformMessage,
    result?.reason,
    result?.error,
    payload.platform_message,
    `publisher risk signal: ${first(result?.riskReason, result?.risk_reason, payload.risk_reason)}`,
  );
}

function candidatesDetail(bookName, observation, candidates) {
  const lines = [
    `reconcile-create found no exact title for "${bookName}" (${first(observation?.reason, 'unknown')}); `
      + `${candidates.length} candidate card(s):`,
  ];
  candidates.forEach((candidate, index) => {
    lines.push(
      `${index + 1}) id ${first(candidate?.remote_book_id) || '-'} ${first(candidate?.remote_url) || '-'} `
        + `"${storableText(first(candidate?.text).replace(/\s+/g, ' '), MAX_CANDIDATE_TEXT)}"`,
    );
  });
  return truncate(lines.join('\n'));
}

function sameBook(left, right) {
  return Boolean(text(left)) && text(left) === text(right);
}

// --- the executor -------------------------------------------------------------------------

export class HubExecutor {
  constructor(controller, deps) {
    this.controller = controller;
    this.deps = deps;
    const interval = Number(deps.hubHeartbeatIntervalMs);
    this.heartbeatIntervalMs = interval > 0 ? interval : HUB_HEARTBEAT_INTERVAL_MS;
  }

  isConfigured(settings) {
    return Boolean(this.deps.hubBackend) && isHubConfigured(settings);
  }

  get journal() {
    return this.controller._requireUploadJournal();
  }

  get client() {
    return this.deps.hubBackend;
  }

  nowMs() {
    return typeof this.deps.nowMs === 'function' ? Number(this.deps.nowMs()) : Date.now();
  }

  async clientId() {
    return text(await this.deps.getHubClientId());
  }

  async sendExecutorHeartbeat() {
    return this.client.heartbeat({
      client_id: await this.clientId(),
      platforms: [...HUB_PLATFORMS],
      version: text(this.deps.getExtensionVersion()),
    });
  }

  // Finishes what the journal holds for publish-hub attempts. Stops at the first
  // error that is not a fence (the hub is unreachable, say): the entry stays.
  async replayJournal() {
    const entries = (await this.journal.pending()).filter((entry) => entry.backend === HUB_BACKEND);
    for (const entry of entries) {
      await this._drive(entry);
    }
    return { handled: entries.length };
  }

  // One claim. {claimed:false} when there is nothing to do this round, including the
  // hub's 409 attempt_in_progress (it still holds an unfinished attempt of ours).
  async claimAndExecute(connectedPlatforms = []) {
    const platforms = HUB_PLATFORMS.filter((platform) => connectedPlatforms.includes(platform));
    if (!platforms.length) {
      return { claimed: false };
    }
    const clientId = await this.clientId();
    let raw;
    try {
      raw = await this.client.claim({ client_id: clientId, platforms });
    } catch (error) {
      if (error?.code === 'attempt_in_progress') {
        return { claimed: false, waiting: true };
      }
      throw error;
    }
    if (!raw) {
      return { claimed: false };
    }
    const claim = await translateHubClaim(raw);
    const existing = await this.journal.find(claim.attempt.attempt_id, HUB_BACKEND);
    if (existing) {
      return { claimed: true, ...(await this._drive(existing)) };
    }
    const entry = await this.journal.recordClaim({ clientId, claim, backend: HUB_BACKEND });
    return { claimed: true, ...(await this._execute(entry)) };
  }

  async _execute(entry) {
    const taskKey = `hub:${entry.job.job_id}:${entry.attempt.attempt_id}`;
    const heartbeat = this._startHeartbeat(entry, taskKey);
    try {
      if (entry.execution_mode === 'reconcile') {
        return await this._reconcile(entry, taskKey, heartbeat);
      }
      return await this._executeMutation(entry, taskKey, heartbeat);
    } finally {
      heartbeat.stop();
      await this.controller.cleanupExecutionTabs(taskKey).catch(() => ({ attempted: false }));
    }
  }

  _startHeartbeat(entry, taskKey) {
    let active = true;
    let fenced = '';
    let timerId = null;
    const schedule = () => {
      if (!active || fenced) {
        return;
      }
      timerId = globalThis.setTimeout(async () => {
        try {
          await this._attemptCall(entry, 'heartbeat', {});
        } catch (error) {
          if (isFencedError(error)) {
            // The hub took the job away: close its tabs so the page stops, and
            // report nothing more for it.
            fenced = String(error.code);
            await this.controller.cleanupExecutionTabs(taskKey).catch(() => ({ attempted: false }));
            return;
          }
          // Anything else (the tunnel is down, say) is retried on the next beat;
          // the next fenced call tells whether the lease is still ours.
        }
        schedule();
      }, this.heartbeatIntervalMs);
      timerId?.unref?.();
    };
    schedule();
    return {
      fenced: () => fenced,
      stop() {
        active = false;
        if (timerId) {
          globalThis.clearTimeout(timerId);
          timerId = null;
        }
      },
    };
  }

  async _attemptCall(entry, action, payload) {
    return this.client.attempt(entry.job.job_id, entry.attempt.attempt_id, action, {
      ...fence(entry),
      ...payload,
    });
  }

  async _openTab(entry, taskKey, url) {
    const tab = await this.deps.openUploadTab(url);
    this.controller.registerExecutionTask(taskKey, tab.tabId);
    await this.controller.waitForOpenedUploadTab(tab.tabId, entry.job.platform, 8000);
    return tab.tabId;
  }

  _commandFor(taskKind) {
    return {
      chapter_upload: this.deps.runUploadCommand,
      cover_upload: this.deps.runCoverUploadCommand,
      create_book: this.deps.runCreateBookCommand,
    }[taskKind];
  }

  async _executeMutation(entry, taskKey, heartbeat) {
    const { job } = entry;
    const attemptId = entry.attempt.attempt_id;
    const blocked = hubPreflight(job);
    if (blocked?.outcome === 'defer') {
      return this._finishWithResult(entry, 'defer', {
        error: blocked.error,
        defer_until: new Date(this.nowMs() + HUB_DEFER_MS).toISOString(),
      });
    }
    if (blocked) {
      return this._finishWithResult(entry, 'permanent_error', { error: blocked.error });
    }

    // Everything up to the phase report happens before any mutation: a failure
    // here is a plain retry (publish-hub backs off and hands the job out again).
    let tabId;
    try {
      const command = this._commandFor(job.task_kind);
      if (typeof command !== 'function') {
        return this._finishWithResult(entry, 'retryable_error', {
          error: { code: 'extension-command-missing', message: `No browser command for ${job.task_kind}.` },
        });
      }
      tabId = await this._openTab(entry, taskKey, hubTargetUrl(job));
      if (job.task_kind === 'create_book') {
        const notReady = await this._createPageProblem(tabId, job.platform);
        if (notReady) {
          return this._finishWithResult(entry, 'retryable_error', { error: notReady });
        }
      }
      if (heartbeat.fenced()) {
        return this._abandon(entry, heartbeat.fenced());
      }
      const risk = await this._preMutationRisk(tabId, job.platform);
      if (risk.pause) {
        return this._finishWithPause(entry, risk.pause.reason, risk.pause.detail);
      }
      if (risk.blocker) {
        return this._finishWithResult(entry, 'retryable_error', {
          error: platformError(risk.blocker, 'publisher-risk-inspection-failed'),
        });
      }
    } catch (error) {
      return this._finishWithResult(entry, 'retryable_error', {
        error: { code: 'extension-preparation-failed', message: errorMessage(error) },
      });
    }
    if (heartbeat.fenced()) {
      return this._abandon(entry, heartbeat.fenced());
    }

    await this.journal.markMutationStarted(attemptId, HUB_BACKEND);
    entry = await this.journal.get(attemptId, HUB_BACKEND);
    try {
      await this._attemptCall(entry, 'phase', { phase: 'mutation_started' });
    } catch (error) {
      if (isFencedError(error)) {
        return this._abandon(entry, error.code);
      }
      // Not acknowledged: nothing is sent to the page.
      return this._finishWithResult(entry, 'retryable_error', {
        error: { code: 'mutation-started-not-acknowledged', message: errorMessage(error) },
      });
    }
    if (heartbeat.fenced()) {
      return this._abandon(entry, heartbeat.fenced());
    }

    const platformResult = await this._runMutation(tabId, job);
    if (heartbeat.fenced()) {
      return this._abandon(entry, heartbeat.fenced());
    }
    return this._finishMutation(entry, platformResult);
  }

  async _createPageProblem(tabId, platform) {
    const waitFor = this.deps.waitForRunnableTab;
    const ready = typeof waitFor === 'function'
      ? await waitFor(platform, tabId, 15000).catch(() => null)
      : null;
    const tab = await this.deps.getTab(tabId);
    const currentUrl = text(tab?.url);
    if (ready?.ready && currentUrl.includes(CREATE_PAGE_MARKERS[platform])) {
      return null;
    }
    return {
      code: 'create-page-not-ready',
      message: 'The platform create page did not become ready (not logged in, or redirected).',
      current_url: currentUrl,
    };
  }

  async _preMutationRisk(tabId, platform) {
    let signal;
    try {
      signal = await this.deps.inspectPlatformRiskCommand(tabId, { platform, boundary: 'pre-mutation' });
    } catch (error) {
      signal = { ok: false, errorCode: 'publisher-risk-inspection-failed', error: errorMessage(error) };
    }
    const blocker = guardRiskInspection(signal, 'pre-mutation');
    if (!blocker) {
      return {};
    }
    if (blocker.riskPause || blocker.risk_pause) {
      return { pause: { reason: hubPauseReason(first(blocker.riskReason, blocker.risk_reason)), detail: riskDetail(blocker) } };
    }
    return { blocker };
  }

  async _runMutation(tabId, job) {
    const command = this._commandFor(job.task_kind);
    const payload = job.task_kind === 'create_book'
      ? { ...job.input }
      : {
        platform: job.platform,
        task_kind: job.task_kind,
        content_sha256: job.content_sha256,
        ...job.input,
      };
    try {
      return await command(tabId, payload);
    } catch (error) {
      return { ok: false, errorCode: 'extension-command-failed', error: errorMessage(error) };
    }
  }

  // Reports after mutation_started. Only an explicit platform refusal is permanent.
  async _finishMutation(entry, result) {
    const { job } = entry;
    const riskReason = riskReasonOf(result);
    if (riskReason !== null) {
      return this._finishWithPause(entry, hubPauseReason(riskReason), riskDetail(result));
    }
    const observedAt = new Date(this.nowMs()).toISOString();
    if (job.task_kind === 'create_book') {
      return this._finishCreate(entry, result, observedAt);
    }
    if (!result?.ok) {
      return this._finishWithResult(entry, 'retryable_error', {
        error: platformError(result, 'extension-execution-failed'),
      });
    }
    let legacy;
    try {
      legacy = buildExecutionReceipt({ job, result, observedAt });
    } catch (error) {
      return this._finishWithResult(entry, 'retryable_error', {
        error: { code: 'receipt-evidence-missing', message: errorMessage(error), current_url: first(result?.currentUrl) },
      });
    }
    if (job.task_kind === 'chapter_upload' && !sameBook(legacy.remote_book_id, job.hub.destination_remote_id)) {
      return this._finishWithPause(
        entry,
        'needs_human',
        `Chapter "${job.input.chapter_title}" was saved in remote book ${legacy.remote_book_id || '(unknown)'} `
          + `(chapter ${legacy.remote_chapter_id}), but the destination is book ${job.hub.destination_remote_id}.`,
      );
    }
    return this._finishWithReceipt(entry, hubReceiptFromLegacy(job, legacy));
  }

  async _finishCreate(entry, result, observedAt) {
    const payload = resultPayloadOf(result);
    const remoteBookId = first(result?.remote_book_id, payload.remote_book_id);
    if (result?.ok && remoteBookId) {
      return this._finishWithReceipt(entry, createReceipt(
        remoteBookId,
        first(result?.remote_url, payload.remote_url, result?.currentUrl),
        {
          verified_via: text(payload.verified_via),
          cover_deferred: Boolean(payload.cover_deferred),
          current_url: first(result?.currentUrl),
        },
        observedAt,
      ));
    }
    const code = first(result?.errorCode, result?.error_code);
    if (!result?.ok && payload.submitted === true && CREATE_PLATFORM_REFUSALS.has(code)) {
      // The platform printed a refusal after the click and no creation evidence
      // was visible (Task 7): nothing was created.
      return this._finishWithResult(entry, 'permanent_error', { error: platformError(result, code) });
    }
    // create-book-outcome-unknown, create-book-already-running, a timeout, a lost
    // reply, a refusal before the click (already after mutation_started): publish-hub
    // reconciles; never a second create from here.
    return this._finishWithResult(entry, 'retryable_error', {
      error: platformError(result, result?.ok ? 'create-book-id-missing' : 'create-book-outcome-unknown'),
    });
  }

  async _reconcile(entry, taskKey, heartbeat) {
    const { job } = entry;
    const readsPlatform = HUB_PLATFORMS.includes(job.platform)
      && (job.task_kind === 'chapter_upload' || (job.task_kind === 'create_book' && job.input.book_name));
    let tabId = 0;
    if (readsPlatform) {
      try {
        const url = job.task_kind === 'create_book'
          ? getPlatformAdapter(job.platform).dashboardUrl
          : hubTargetUrl(job);
        tabId = await this._openTab(entry, taskKey, url);
      } catch (_error) {
        tabId = 0;
      }
    }
    if (heartbeat.fenced()) {
      return this._abandon(entry, heartbeat.fenced());
    }
    try {
      await this._attemptCall(entry, 'phase', { phase: 'observation_started' });
    } catch (error) {
      if (isFencedError(error)) {
        return this._abandon(entry, error.code);
      }
      throw error;
    }
    if (!readsPlatform || !tabId) {
      // Covers cannot be tied to the claimed asset on the page; other kinds are
      // not ours to observe.
      return this._finishWithReconcile(entry, 'unknown');
    }
    const observedAt = new Date(this.nowMs()).toISOString();
    let decision;
    if (job.task_kind === 'create_book') {
      const observation = await this._observe(this.deps.runReconcileCreateCommand, tabId, {
        platform: job.platform,
        book_name: job.input.book_name,
      });
      decision = this._createFinding(job, observation, observedAt);
    } else {
      const observation = await this._observe(this.deps.runReconciliationCommand, tabId, {
        platform: job.platform,
        task_kind: job.task_kind,
        content_sha256: job.content_sha256,
        ...job.input,
      });
      decision = this._uploadFinding(job, observation, observedAt);
    }
    if (heartbeat.fenced()) {
      return this._abandon(entry, heartbeat.fenced());
    }
    if (decision.pause) {
      return this._finishWithPause(entry, decision.pause.reason, decision.pause.detail);
    }
    if (decision.finding === 'found') {
      return this._finishWithReceipt(entry, decision.receipt);
    }
    return this._finishWithReconcile(entry, decision.finding);
  }

  async _observe(command, tabId, payload) {
    if (typeof command !== 'function') {
      return { outcome: 'indeterminate', finding: 'unknown', reason: 'no read-only command' };
    }
    try {
      return await command(tabId, payload);
    } catch (error) {
      return { outcome: 'indeterminate', finding: 'unknown', reason: errorMessage(error) };
    }
  }

  // reconcile-upload: matched -> found (receipt first), anything else -> unknown,
  // risk_pause (checked first) -> pause. A match in another book is unknown.
  _uploadFinding(job, observation, observedAt) {
    if (text(observation?.outcome) === 'risk_pause') {
      return { pause: { reason: hubPauseReason(first(observation.riskReason, observation.risk_reason)), detail: riskDetail(observation) } };
    }
    const request = buildReconciliationRequest({ job, observation, observedAt });
    if (
      request.outcome === 'matched'
      && request.receipt
      && sameBook(request.receipt.remote_book_id, job.hub.destination_remote_id)
    ) {
      return { finding: 'found', receipt: hubReceiptFromLegacy(job, request.receipt) };
    }
    return { finding: 'unknown' };
  }

  // reconcile-create (Task 7 answers unknown today). absent only with a positive
  // "works list complete" signal, because a wrong absent creates a second book. An
  // unknown that came with candidate cards pauses for a human with the candidates
  // in the detail (ruling R48): the job ends paused either way, but /ops shows them.
  _createFinding(job, observation, observedAt) {
    if (text(observation?.outcome) === 'risk_pause') {
      return { pause: { reason: hubPauseReason(first(observation.riskReason, observation.risk_reason)), detail: riskDetail(observation) } };
    }
    const finding = text(observation?.finding);
    const remoteBookId = text(observation?.remote_book_id);
    if (finding === 'found' && remoteBookId) {
      return {
        finding: 'found',
        receipt: createReceipt(remoteBookId, first(observation.remote_url), { verified_via: 'reconcile-create' }, observedAt),
      };
    }
    if (finding === 'absent' && observation?.works_list_complete === true) {
      return { finding: 'absent' };
    }
    const candidates = Array.isArray(observation?.candidates) ? observation.candidates : [];
    if (candidates.length) {
      return { pause: { reason: 'needs_human', detail: candidatesDetail(job.input.book_name, observation, candidates) } };
    }
    return { finding: 'unknown' };
  }

  // --- journaled reports ---------------------------------------------------------------

  async _finishWithResult(entry, outcome, extras) {
    const saved = await this.journal.saveResult(
      entry.attempt.attempt_id,
      resultRecord(entry, outcome, extras),
      HUB_BACKEND,
    );
    return this._drive(saved);
  }

  async _finishWithReconcile(entry, finding) {
    const saved = await this.journal.saveResult(
      entry.attempt.attempt_id,
      reconcileRecord(entry, finding),
      HUB_BACKEND,
    );
    return this._drive(saved);
  }

  async _finishWithPause(entry, reason, detail) {
    const saved = await this.journal.savePause(
      entry.attempt.attempt_id,
      pauseRecord(entry, reason, detail),
      HUB_BACKEND,
    );
    return this._drive(saved);
  }

  async _finishWithReceipt(entry, receipt) {
    const saved = await this.journal.saveReceipt(
      entry.attempt.attempt_id,
      receiptRecord(entry, receipt),
      HUB_BACKEND,
    );
    return this._drive(saved);
  }

  async _abandon(entry, reason) {
    await this.journal.remove(entry.attempt.attempt_id, HUB_BACKEND);
    return { status: 'abandoned', reason };
  }

  // Sends what the journal holds for one attempt, in order: a pause, else the final
  // result/reconcile, else the receipt and then the success that follows it. The
  // final report is stored only once its receipt was acknowledged. An attempt with
  // nothing to report (the worker stopped) is finished from its local phase.
  async _drive(entry) {
    const attemptId = entry.attempt.attempt_id;
    let current = entry;
    for (let step = 0; step < 4; step += 1) {
      if (current.local_phase === 'acked') {
        return { status: 'reported' };
      }
      if (current.pause) {
        return this._sendFinal(current, 'pause', current.pause.body);
      }
      if (current.result) {
        return this._sendFinal(current, current.result.report, current.result.body);
      }
      if (current.receipt) {
        try {
          await this.client.attempt(current.job.job_id, attemptId, 'receipt', current.receipt.body);
        } catch (error) {
          if (isFencedError(error)) {
            return this._abandon(current, error.code);
          }
          if (error?.code === 'remote_id_conflict') {
            current = await this.journal.savePause(
              attemptId,
              pauseRecord(current, 'needs_human', `remote_id_conflict: receipt remote_id ${current.receipt.body.remote_id || '(none)'} `
                + `was refused: ${errorMessage(error)}`),
              HUB_BACKEND,
            );
            continue;
          }
          throw error;
        }
        current = await this.journal.saveResult(
          attemptId,
          current.execution_mode === 'reconcile'
            ? reconcileRecord(current, 'found')
            : resultRecord(current, 'succeeded'),
          HUB_BACKEND,
        );
        continue;
      }
      if (current.execution_mode === 'execute' && current.local_phase === 'mutation_started') {
        current = await this.journal.saveResult(attemptId, resultRecord(current, 'retryable_error', {
          error: {
            code: 'extension-worker-restarted',
            message: 'The extension stopped after mutation_started, before a result was recorded.',
          },
        }), HUB_BACKEND);
        continue;
      }
      // Claimed and nothing more: the platform was not touched. publish-hub hands
      // the attempt out again or lets its lease expire; nothing to report.
      await this.journal.remove(attemptId, HUB_BACKEND);
      return { status: 'dropped' };
    }
    throw new Error(`publish-hub journal entry ${attemptId} did not settle.`);
  }

  async _sendFinal(entry, report, body) {
    const attemptId = entry.attempt.attempt_id;
    try {
      await this.client.attempt(entry.job.job_id, attemptId, report, body);
    } catch (error) {
      if (isFencedError(error)) {
        return this._abandon(entry, error.code);
      }
      if (report === 'reconcile' && error?.code === 'remote_id_on_record' && body.finding !== 'unknown') {
        try {
          await this.client.attempt(entry.job.job_id, attemptId, 'reconcile', { ...body, finding: 'unknown' });
        } catch (retryError) {
          if (isFencedError(retryError)) {
            return this._abandon(entry, retryError.code);
          }
          throw retryError;
        }
      } else {
        throw error;
      }
    }
    await this.journal.markAcknowledged(attemptId, HUB_BACKEND);
    await this.journal.compact();
    return { status: 'reported', report, outcome: report === 'pause' ? body.reason : (body.outcome || body.finding) };
  }
}
