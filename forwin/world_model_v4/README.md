# forwin.world_model_v4

Status: COMPATIBILITY projection / migration source / debug bridge.

`world_model_v4` is the old V4 ledger path for world deltas, beliefs, gaps, reveals, reader experience, debug/export, and BookState adapter bridge. It exists because V4 was implemented side-by-side before BookState became the final canon runtime.

Rules:

- Do not add final canon features here.
- New accepted-chapter canon must succeed through `BookStateReviewGate -> BookStateCompiler`.
- This package may write compatibility projection rows only when the world_v4 compatibility path is enabled.
- Prefer new imports through `forwin.world_v4_compat` after the alias migration.
