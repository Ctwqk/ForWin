# forwin.reviewer

Status: DRAFT REVIEW domain.

`reviewer` owns chapter-draft signal collection. `DraftReviewService` aggregates continuity, governance, experience, map movement, personality, lint, and webnovel-facing review signals.

Rules:

- Treat this package as the main review surface for chapter drafts.
- Keep world_v4 extraction-specific checks in the compatibility gate, not in this facade.
- Do not replace `DraftReviewService` with `reviewer_v4`; the latter is a compatibility gate.
