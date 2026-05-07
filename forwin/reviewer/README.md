# forwin.reviewer

Status: MAIN REVIEW facade.

`reviewer` owns the normal chapter-level review facade. `HistoricalReviewHub` aggregates continuity, governance, experience, map movement, personality, lint, and webnovel-facing review signals.

Rules:

- Treat this package as the main review surface for chapter drafts.
- Keep world_v4 extraction-specific checks in the compatibility gate, not in this facade.
- Do not replace `HistoricalReviewHub` with `reviewer_v4`; the latter is a compatibility gate.
