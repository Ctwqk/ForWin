# forwin.review

Status: DRAFT REVIEW domain.

`review` owns chapter-draft signal collection, decision rules, repair execution, verification, and final residual policy. `DraftReviewService` aggregates continuity, plan-contract, experience, map movement, personality, lint, and webnovel-facing review signals.

Rules:

- Treat this package as the main review surface for chapter drafts.
- BookState extraction checks belong to `forwin.book_state.extraction`; its deterministic gate validates deltas before Canon admission.
- Review the complete stitched `WriterOutput.body` as the final narrative. Pre-stitch scene drafts remain writing artifacts and must not be supplied as a second narrative to the LLM reviewer.
- Preserve protected chapter titles during repair unless the plan explicitly changes them; this does not weaken content verification.
