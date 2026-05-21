# forwin.world_model

Status: deprecated projection / wiki / export facade.

`world_model` preserves the pre-BookState world snapshot/export path for
operator inspection and import/export workflows. BookState remains the canon
source for accepted-chapter runtime state.

Rules:

- Do not add new final canon semantics here.
- Do not use `WorldModelCompiler` as the accepted-chapter canon writer for new runtime paths.
- New source-of-truth behavior belongs in `forwin.book_state`.
- Current API routes may keep this package as a read/export facade while
  operator inspection and audit support still need it.
- Accepted-chapter runtime projection writes have been removed; this package must not be reintroduced into chapter acceptance.
