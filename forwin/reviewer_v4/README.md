# forwin.reviewer_v4

Status: COMPATIBILITY gate.

`reviewer_v4` reviews extracted world_v4 changes before the old V4 compatibility compiler/projection path. It is not the main chapter reviewer and must not grow into a replacement for `forwin.reviewer`.

Rules:

- Use only for `WorldDeltaExtractor -> world_v4 review gate -> compatibility projection` flows.
- BookState direct path should use BookState-native extraction and `BookStateReviewGate`.
- Prefer new imports through `forwin.world_v4_review_gate` after the alias migration.
