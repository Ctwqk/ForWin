# forwin.book_state

Status: CANON runtime.

`book_state` owns the final BookState runtime: GraphDelta ledger, direct extraction contract, deterministic review gate, compiler, snapshot/replay, and projection.

Rules:

- `BookStateCompiler` is the only final GraphDelta canon writer.
- `BookStateReviewGate` must run before new chapter changes enter canon.
- `BookStateDirectCommitService` is the direct accepted-chapter commit helper used by orchestration.
- Obsidian, LLM KB, wiki/export, and debug APIs must be rebuildable projections from BookState or declared integration rows.
