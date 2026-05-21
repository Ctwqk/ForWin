# forwin.world_model

Status: LEGACY projection / wiki / export layer.

`world_model` preserves the pre-BookState world snapshot/export path. It may bootstrap, read, export, import, and rebuild legacy projection views for compatibility and operator inspection.

Rules:

- Do not add new final canon semantics here.
- Do not use `WorldModelCompiler` as the accepted-chapter canon writer for new runtime paths.
- New source-of-truth behavior belongs in `forwin.book_state`.
- Legacy APIs may keep this package as a read/export facade while migration and audit support still need it.
- Accepted-chapter runtime projection writes have been removed; this package must not be reintroduced into chapter acceptance.
