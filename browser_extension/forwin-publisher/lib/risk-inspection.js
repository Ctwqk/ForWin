export function guardRiskInspection(inspection, boundary = 'pre-mutation') {
  if (inspection?.riskPause || inspection?.risk_pause) {
    return inspection;
  }
  if (inspection?.ok !== false) {
    return null;
  }
  const priorPayload = inspection?.resultPayload && typeof inspection.resultPayload === 'object'
    ? inspection.resultPayload
    : {};
  return {
    ...inspection,
    ok: false,
    riskPause: false,
    errorCode: String(inspection?.errorCode || 'publisher-risk-inspection-failed'),
    error: String(
      inspection?.error
      || 'Publisher risk inspection failed before a trusted mutation.',
    ),
    resultPayload: {
      ...priorPayload,
      phase: 'risk-inspection-failed',
      inspection_phase: String(priorPayload.phase || ''),
      boundary: String(boundary || 'pre-mutation'),
    },
  };
}
