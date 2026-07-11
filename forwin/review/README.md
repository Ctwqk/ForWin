# forwin.review

Status: DRAFT REVIEW domain.

`review` owns chapter-draft signal collection, decision rules, repair execution, verification, and final residual policy. `DraftReviewService` aggregates continuity, plan-contract, experience, map movement, personality, lint, and webnovel-facing review signals.

Rules:

- Treat this package as the main review surface for chapter drafts.
- Keep world_v4 extraction-specific checks in the compatibility gate, not in this facade.
- Do not replace `DraftReviewService` with `world_v4_review_gate`; the latter validates BookState extraction deltas.
