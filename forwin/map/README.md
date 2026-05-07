# forwin.map

Status: CANON map runtime.

`map` owns Scheme C BookMap: generation, repository access, validation, pathfinding, map visibility, movement support, and Genesis map_atlas handoff.

Rules:

- BookMap semantics are `SubWorld -> Region -> MapNode -> MapEdge`.
- `SubWorld` is only a large-scale container such as continent, planet, plane, otherworld, or star sector.
- Cities, inns, sect branches, ruin entrances, towers, rooms, and local sites must be represented as `Region`, `MapNode`, or `site_state`, not as new `SubWorld` meanings.
- Writer context may expose visible map data; reviewer-only objective route data must stay out of writer-facing context.
