function text(value) {
  return String(value || '').trim();
}

function first(...values) {
  return values.map(text).find(Boolean) || '';
}

const RISK_REASONS = new Set(['captcha', 'mfa', 'account_risk']);

function resultPayload(result) {
  const payload = result?.resultPayload || result?.result_payload;
  return payload && typeof payload === 'object' ? payload : {};
}

function canonicalUrl(parsed, query = '') {
  const path = parsed.pathname.replace(/\/+$/, '') || '/';
  return `${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}${path}${query ? `?${query}` : ''}`;
}

export function extractRemoteIdentity(platform, rawUrl) {
  const value = text(rawUrl);
  if (!value) {
    return { remote_book_id: '', remote_chapter_id: '', remote_url: '' };
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch (_error) {
    return { remote_book_id: '', remote_chapter_id: '', remote_url: '' };
  }

  const platformId = text(platform);
  const hostname = parsed.hostname.toLowerCase();
  let remoteBookId = '';
  let remoteChapterId = '';
  let query = '';
  if (platformId === 'qidian') {
    if (hostname !== 'write.qq.com' && !hostname.endsWith('.write.qq.com')) {
      return { remote_book_id: '', remote_chapter_id: '', remote_url: '' };
    }
    const fragment = new URLSearchParams(parsed.hash.replace(/^#/, ''));
    const candidate = parsed.searchParams.get('ccid') || fragment.get('ccid') || '';
    remoteChapterId = /^\d+$/.test(candidate) ? candidate : '';
    remoteBookId = parsed.pathname.match(/\/CBID\/(\d+)(?:\/|$)/i)?.[1]
      || parsed.pathname.match(/\/portal\/book\/([^/?#]+)(?:\/|$)/i)?.[1]
      || '';
    query = remoteChapterId ? new URLSearchParams({ ccid: remoteChapterId }).toString() : '';
  } else if (platformId === 'fanqie') {
    if (hostname !== 'fanqienovel.com' && !hostname.endsWith('.fanqienovel.com')) {
      return { remote_book_id: '', remote_chapter_id: '', remote_url: '' };
    }
    const chapterPath = parsed.pathname.match(
      /\/main\/writer\/(?:chapter-edit|chapter-detail)\/(\d+)\/(\d+)/,
    );
    remoteBookId = parsed.pathname.match(
      /\/main\/writer\/(?:chapter-manage|book-info)\/(\d+)/,
    )?.[1]
      || parsed.pathname.match(/\/main\/writer\/(\d+)\/publish(?:\/|$)/)?.[1]
      || chapterPath?.[1]
      || '';
    remoteChapterId = first(
      parsed.searchParams.get('chapter_id'),
      parsed.searchParams.get('item_id'),
      chapterPath?.[2],
    );
    query = remoteChapterId
      ? new URLSearchParams({ chapter_id: remoteChapterId }).toString()
      : '';
  } else {
    return { remote_book_id: '', remote_chapter_id: '', remote_url: '' };
  }
  return {
    remote_book_id: remoteBookId,
    remote_chapter_id: remoteChapterId,
    remote_url: canonicalUrl(parsed, query),
  };
}

function officialState(job, result, override = '') {
  const payload = resultPayload(result);
  const raw = first(
    override,
    result?.officialState,
    result?.official_state,
    payload.official_state,
  ).toLowerCase().replaceAll('-', '_');
  if (job.task_kind === 'cover_upload') {
    return 'cover_uploaded';
  }
  if (raw === 'published') {
    return 'published';
  }
  if (['review_pending', 'under_review'].includes(raw)) {
    return 'review_pending';
  }
  return 'drafted';
}

function receiptIdentity(job, candidate) {
  const payload = resultPayload(candidate);
  const rawUrl = first(
    candidate?.currentUrl,
    candidate?.current_url,
    candidate?.remoteUrl,
    candidate?.remote_url,
    payload.remote_url,
    job.input?.remote_url,
  );
  const parsed = extractRemoteIdentity(job.platform, rawUrl);
  return {
    remote_book_id: first(
      candidate?.remoteBookId,
      candidate?.remote_book_id,
      payload.remote_book_id,
      job.input?.remote_book_id,
      parsed.remote_book_id,
    ),
    remote_chapter_id: first(
      candidate?.remoteChapterId,
      candidate?.remote_chapter_id,
      payload.remote_chapter_id,
      parsed.remote_chapter_id,
    ),
    remote_url: parsed.remote_url || rawUrl,
  };
}

function coverProof(job, candidate) {
  const payload = resultPayload(candidate);
  const currentUrl = first(
    candidate?.currentUrl,
    candidate?.current_url,
    candidate?.remoteUrl,
    candidate?.remote_url,
    payload.remote_url,
  );
  const parsed = extractRemoteIdentity(job.platform, currentUrl);
  const observedRemoteBookId = first(
    candidate?.remoteBookId,
    candidate?.remote_book_id,
    payload.remote_book_id,
    parsed.remote_book_id,
  );
  const matchBasis = Array.isArray(candidate?.matchBasis)
    ? candidate.matchBasis
    : Array.isArray(candidate?.match_basis)
      ? candidate.match_basis
      : [];
  const accepted = Boolean(
    candidate?.platformAccepted === true
    || candidate?.platform_accepted === true
    || first(candidate?.acceptanceSignal, candidate?.acceptance_signal, payload.acceptance_signal)
    || matchBasis.map(text).includes('platform_acceptance'),
  );
  return {
    accepted,
    expectedRemoteBookId: text(job.input?.remote_book_id),
    observedRemoteBookId,
  };
}

function chapterProof(job, candidate) {
  const payload = resultPayload(candidate);
  const expectedHash = text(job.content_sha256).toLowerCase();
  const matchedHash = first(
    candidate?.matchedContentSha256,
    candidate?.matched_content_sha256,
  ).toLowerCase();
  const matchBasis = Array.isArray(candidate?.matchBasis)
    ? candidate.matchBasis.map(text)
    : Array.isArray(candidate?.match_basis)
      ? candidate.match_basis.map(text)
      : [];
  if (matchedHash === expectedHash && matchBasis.includes('content_sha256')) {
    return true;
  }
  const observedHash = text(payload.observed_content_sha256).toLowerCase();
  const expectedNormalizedHash = text(payload.expected_normalized_sha256).toLowerCase();
  const observedNormalizedHash = text(payload.observed_normalized_sha256).toLowerCase();
  return observedHash === expectedHash
    && /^[0-9a-f]{64}$/.test(expectedNormalizedHash)
    && expectedNormalizedHash === observedNormalizedHash
    && payload.content_match_basis === 'normalized-editor-text-sha256';
}

export function buildExecutionReceipt({ job, result, observedAt }) {
  const identity = receiptIdentity(job, result);
  if (
    job.task_kind === 'chapter_upload'
    && (!identity.remote_book_id || !identity.remote_chapter_id)
  ) {
      throw new Error('Chapter receipt requires stable remote book and chapter identity.');
  }
  if (job.task_kind === 'chapter_upload' && !chapterProof(job, result)) {
    throw new Error('Chapter receipt requires observed content evidence matching the claim.');
  }
  if (!identity.remote_book_id) {
    throw new Error('Publisher receipt requires stable remote book identity.');
  }
  if (job.task_kind === 'cover_upload') {
    const proof = coverProof(job, result);
    if (
      !proof.expectedRemoteBookId
      || proof.observedRemoteBookId !== proof.expectedRemoteBookId
    ) {
      throw new Error('Cover receipt requires the observed remote book identity to match the claim.');
    }
    if (!proof.accepted) {
      throw new Error('Cover receipt requires explicit platform acceptance evidence.');
    }
  }
  const payload = resultPayload(result);
  const confirmationText = first(
    result?.confirmationText,
    result?.confirmation_text,
    result?.message,
    payload.confirmation_text,
    payload.official_status,
    payload.cover_state,
  );
  const platformMessage = first(
    result?.platformMessage,
    result?.platform_message,
    payload.platform_message,
  );
  return {
    content_sha256: text(job.content_sha256).toLowerCase(),
    ...identity,
    official_state: officialState(job, result),
    observed_at: text(observedAt),
    evidence: {
      selector: first(result?.selector, payload.selector),
      heading: first(result?.heading, job.input?.chapter_title, job.input?.book_name),
      matched: true,
      content_sha256: text(job.content_sha256).toLowerCase(),
      confirmation_text: confirmationText,
      platform_message: platformMessage,
      screenshot_sha256: first(result?.screenshotSha256, result?.screenshot_sha256),
    },
  };
}

function coverDetails(payload) {
  const rawCoverState = text(payload.cover_state).toLowerCase().replaceAll('-', '_');
  const coverState = ['uploaded', 'under_review', 'approved', 'rejected'].includes(rawCoverState)
    ? rawCoverState
    : 'uploaded';
  const rawAuditState = text(payload.audit_state).toLowerCase().replaceAll('-', '_');
  const auditState = ['', 'unknown', 'under_review', 'approved', 'rejected'].includes(rawAuditState)
    ? rawAuditState
    : '';
  return {
    cover_state: coverState,
    audit_state: auditState,
    platform_message: text(payload.platform_message),
  };
}

function auditDetails(payload) {
  return {
    work: payload.work && typeof payload.work === 'object' ? payload.work : {},
    chapters: Array.isArray(payload.chapters) ? payload.chapters : [],
    cover: payload.cover && typeof payload.cover === 'object' ? payload.cover : {},
    milestones: Array.isArray(payload.milestones) ? payload.milestones : [],
  };
}

export function buildAttemptResult({ job, result }) {
  const explicitlyCancelled = result?.outcome === 'cancelled' || result?.cancelled === true;
  const outcome = explicitlyCancelled ? 'cancelled' : result?.ok ? 'succeeded' : 'failed';
  const errorMessage = first(result?.errorMessage, result?.error_message, result?.error);
  const errorCode = first(result?.errorCode, result?.error_code);
  const payload = resultPayload(result);
  let details = {};
  if (outcome === 'succeeded' && job.task_kind === 'cover_upload') {
    details = coverDetails(payload);
  } else if (outcome === 'succeeded' && job.task_kind === 'audit_sync') {
    details = auditDetails(payload);
  }
  return {
    outcome,
    message: first(result?.message, outcome === 'succeeded' ? 'Publisher task completed.' : ''),
    current_url: first(result?.currentUrl, result?.current_url),
    error_code: outcome === 'failed' ? (errorCode || 'extension-execution-failed') : '',
    error_message: outcome === 'failed' ? (errorMessage || 'Publisher task failed.') : '',
    details,
  };
}

export function buildRiskPauseRequest({ signal, observedAt }) {
  const source = signal && typeof signal === 'object' ? signal : {};
  const evidenceSource = source.riskEvidence && typeof source.riskEvidence === 'object'
    ? source.riskEvidence
    : source.risk_evidence && typeof source.risk_evidence === 'object'
      ? source.risk_evidence
      : {};
  const riskReason = first(source.riskReason, source.risk_reason);
  if (!RISK_REASONS.has(riskReason)) {
    throw new Error('Publisher risk pause requires a typed risk reason.');
  }
  return {
    risk_reason: riskReason,
    observed_at: text(observedAt),
    current_url: first(source.currentUrl, source.current_url),
    evidence: {
      detector: first(evidenceSource.detector, 'publisher-risk-v1'),
      boundary: first(evidenceSource.boundary, 'unknown'),
      selector: first(evidenceSource.selector),
      matched_text: first(evidenceSource.matchedText, evidenceSource.matched_text),
      message: first(evidenceSource.message, source.message, source.error),
    },
  };
}

function reconciliationEvidence(observation, overrides = {}) {
  return {
    procedure: first(observation?.procedure, 'read-only-platform-inspection'),
    match: first(observation?.match),
    match_basis: Array.isArray(observation?.matchBasis)
      ? observation.matchBasis.map(text).filter(Boolean)
      : Array.isArray(observation?.match_basis)
        ? observation.match_basis.map(text).filter(Boolean)
        : [],
    selector: first(observation?.selector),
    matched: typeof observation?.matched === 'boolean' ? observation.matched : null,
    matched_content_sha256: first(
      observation?.matchedContentSha256,
      observation?.matched_content_sha256,
    ).toLowerCase(),
    pagination_complete: typeof observation?.paginationComplete === 'boolean'
      ? observation.paginationComplete
      : typeof observation?.pagination_complete === 'boolean'
        ? observation.pagination_complete
        : null,
    reason: first(overrides.reason, observation?.reason),
    platform_message: first(observation?.platformMessage, observation?.platform_message),
    risk_reason: first(overrides.riskReason),
  };
}

export function buildReconciliationRequest({
  job,
  observation,
  observedAt,
}) {
  const requestedOutcome = first(observation?.outcome, 'indeterminate');
  let outcome = ['matched', 'absent', 'indeterminate', 'risk_pause'].includes(requestedOutcome)
    ? requestedOutcome
    : 'indeterminate';
  let errorCode = first(observation?.errorCode, observation?.error_code);
  let errorMessage = first(observation?.errorMessage, observation?.error_message);
  let reason = first(observation?.reason);
  let riskReason = first(observation?.riskReason, observation?.risk_reason);
  const matchedHash = first(
    observation?.matchedContentSha256,
    observation?.matched_content_sha256,
  ).toLowerCase();
  const expectedHash = text(job.content_sha256).toLowerCase();
  let receipt;

  if (outcome === 'matched' && job.task_kind === 'cover_upload') {
    outcome = 'indeterminate';
    errorCode = 'cover-asset-hash-unobservable';
    errorMessage = 'Read-only cover state cannot be bound to the claimed asset hash.';
    reason = errorMessage;
  } else if (outcome === 'matched' && matchedHash !== expectedHash) {
    outcome = 'indeterminate';
    errorCode = 'content-hash-mismatch';
    errorMessage = 'Read-only evidence did not match the claimed content hash.';
    reason = errorMessage;
  }
  if (outcome === 'absent') {
    outcome = 'indeterminate';
    errorCode = 'authoritative-absence-disabled';
    errorMessage = 'Authoritative absence is not enabled by backend policy.';
    reason = errorMessage;
  }
  if (outcome === 'risk_pause' && !RISK_REASONS.has(riskReason)) {
    outcome = 'indeterminate';
    riskReason = '';
    errorCode = 'risk-reason-invalid';
    errorMessage = 'Risk pause evidence did not include a supported typed reason.';
    reason = errorMessage;
  }
  if (outcome === 'matched') {
    try {
      receipt = buildExecutionReceipt({
        job,
        observedAt,
        result: {
          ...observation,
          ok: true,
          officialState: first(observation?.officialState, observation?.official_state),
          currentUrl: first(observation?.currentUrl, observation?.current_url),
          confirmationText: first(observation?.confirmationText, observation?.confirmation_text),
        },
      });
    } catch (error) {
      outcome = 'indeterminate';
      errorCode = 'unstable-remote-identity';
      errorMessage = error instanceof Error ? error.message : String(error);
      reason = errorMessage;
    }
  }

  const evidence = reconciliationEvidence(observation, { reason, riskReason });
  if (outcome === 'matched') {
    evidence.matched = true;
    evidence.matched_content_sha256 = expectedHash;
  }
  return {
    outcome,
    observed_at: text(observedAt),
    current_url: first(observation?.currentUrl, observation?.current_url),
    evidence,
    ...(receipt ? { receipt } : {}),
    error_code: errorCode,
    error_message: errorMessage,
  };
}
