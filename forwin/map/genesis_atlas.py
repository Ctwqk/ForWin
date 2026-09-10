"""Validate authored MapAtlas structure before normalization can fill a scaffold."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .genesis_route import GenesisRoute, GenesisRouteContractError, parse_genesis_routes

_NODE_PARENT_FIELDS = (
    "parent_subworld",
    "parent_subworld_id",
    "subworld_id",
    "subworld_name",
)


class _Place(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, pattern=r"\S")
    name: str = Field(min_length=1, pattern=r"\S")
    culture_profile_id: str = ""


class _Submap(_Place):
    scope: str = "other"
    parent_scope: str = ""
    summary: str = ""
    culture_traits: list[str] = Field(default_factory=list)
    climate: str = ""
    terrain: list[str] = Field(default_factory=list)
    governing_power: str = ""
    resident_factions: list[str] = Field(default_factory=list)
    key_locations: list[str] = Field(default_factory=list)
    travel_rules: list[str] = Field(default_factory=list)
    resource_themes: list[str] = Field(default_factory=list)


class _Region(_Place):
    subworld_name: str = Field(min_length=1, pattern=r"\S")
    parent_region_id: str = ""
    level: Literal[1, 2] = 1
    kind: str = "local_region"
    summary: str = ""
    culture_traits: list[str] = Field(default_factory=list)
    climate: str = ""
    terrain: list[str] = Field(default_factory=list)
    controller_factions: list[str] = Field(default_factory=list)
    resource_themes: list[str] = Field(default_factory=list)


class _Node(_Place):
    parent_subworld: str = Field(min_length=1, pattern=r"\S")
    parent_region_id: str = ""
    kind: str = "other"
    description: str = ""
    control: str = ""
    danger: str = ""
    climate_note: str = ""
    terrain_note: str = ""
    culture_note: str = ""
    resources: list[str] = Field(default_factory=list)


class _Atlas(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    overview: str
    topology_rules: list[str]
    submaps: list[_Submap]
    regions: list[_Region]
    nodes: list[_Node]
    edges: list[dict[str, Any]]


def genesis_map_output_schema(*, canonical_routes: bool = True) -> dict[str, Any]:
    schema = _Atlas.model_json_schema()
    if canonical_routes:
        schema["properties"]["edges"]["items"] = GenesisRoute.model_json_schema(
            by_alias=True
        )
    return schema


def validate_complete_genesis_map(
    payload: Any, *, canonical_routes: bool = True
) -> None:
    """Validate a full model response, preserving its original dictionaries."""
    try:
        _Atlas.model_validate(payload)
    except ValidationError as exc:
        paths = []
        for error in exc.errors():
            path = "world.map_atlas"
            for part in error["loc"]:
                path += f"[{part}]" if isinstance(part, int) else f".{part}"
            paths.append(path)
        raise GenesisRouteContractError(
            ", ".join(paths), "invalid complete map fields", payload
        ) from exc
    validate_genesis_map_references(payload, canonical_routes=canonical_routes)


def validate_genesis_map_references(
    atlas: dict[str, Any], *, canonical_routes: bool = False
) -> None:
    """Check supplied identities/references, including merged legacy edits.

    Missing fields in an initial World scaffold remain possible. Explicit rows
    and references cannot be dropped, renamed, or reassigned by normalization.
    """
    atlas = with_genesis_map_identities(atlas)
    routes = parse_genesis_routes(atlas.get("edges", []), canonical=canonical_routes)
    rows_by_kind = {}
    ids_by_kind = {}
    refs_by_kind = {}

    def fail(path, message, raw):
        raise GenesisRouteContractError("world.map_atlas." + path, message, raw)

    for kind in ("submaps", "regions", "nodes"):
        rows = atlas.get(kind, [])
        if not isinstance(rows, list):
            fail(kind, "expected a list", rows)
        ids, names = {}, {}
        for index, row in enumerate(rows):
            path = f"{kind}[{index}]"
            if not isinstance(row, dict):
                fail(path, "expected an object", row)
            identity = row.get("id", "")
            if not isinstance(identity, str):
                fail(path + ".id", "expected a string", row)
            identity = identity.strip()
            if identity:
                if identity in ids:
                    fail(path + ".id", "duplicate authored identity", row)
                ids[identity] = row
                name = row.get("name", "")
                if isinstance(name, str) and name.strip():
                    names.setdefault(name.strip(), set()).add(identity)
        refs = {key: key for key in ids}
        for name, matches in names.items():
            if len(matches) == 1 and name not in refs:
                refs[name] = next(iter(matches))
        rows_by_kind[kind], ids_by_kind[kind], refs_by_kind[kind] = rows, ids, refs

    def reference(row, key, kind, path):
        value = row.get(key, "")
        if not isinstance(value, str):
            fail(path + "." + key, "expected a reference string", row)
        value = value.strip()
        references = (
            {key: key for key in ids_by_kind[kind]}
            if kind == "regions"
            else refs_by_kind[kind]
        )
        if value and value not in references:
            fail(path + "." + key, "unresolved or ambiguous reference", row)
        return references.get(value, "")

    region_worlds = {}
    for index, region in enumerate(rows_by_kind["regions"]):
        path = f"regions[{index}]"
        world = reference(region, "subworld_name", "submaps", path)
        region_worlds[str(region.get("id", "")).strip()] = world
    for index, region in enumerate(rows_by_kind["regions"]):
        path = f"regions[{index}]"
        parent = reference(region, "parent_region_id", "regions", path)
        level = region.get("level", 1)
        if type(level) is not int or level not in {1, 2}:
            fail(path + ".level", "expected region level 1 or 2", region)
        if level == 1 and parent:
            fail(
                path + ".parent_region_id",
                "level 1 region cannot have a parent",
                region,
            )
        if level == 2:
            if not parent or ids_by_kind["regions"][parent].get("level", 1) != 1:
                fail(
                    path + ".parent_region_id",
                    "level 2 region needs a level 1 parent",
                    region,
                )
            if (
                region_worlds[parent]
                != region_worlds[str(region.get("id", "")).strip()]
            ):
                fail(
                    path + ".parent_region_id",
                    "parent belongs to another SubWorld",
                    region,
                )
    node_worlds = {}
    for index, node in enumerate(rows_by_kind["nodes"]):
        path = f"nodes[{index}]"
        declared_worlds = {
            reference(node, key, "submaps", path)
            for key in _NODE_PARENT_FIELDS
            if key in node
        }
        declared_worlds.discard("")
        if len(declared_worlds) > 1:
            fail(path + ".parent_subworld", "conflicting node parent references", node)
        world = next(iter(declared_worlds), "")
        region = reference(node, "parent_region_id", "regions", path)
        if world and region and region_worlds[region] != world:
            fail(path + ".parent_region_id", "region belongs to another SubWorld", node)
        node_worlds[str(node.get("id", "")).strip()] = world
    for index, parsed in enumerate(routes):
        route = parsed.route
        endpoints = (route.from_ref, route.to_ref)
        nodes = refs_by_kind["nodes"]
        worlds = refs_by_kind["submaps"]
        if not (
            all(ref in nodes for ref in endpoints)
            or all(ref in worlds for ref in endpoints)
        ):
            fail(
                f"edges[{index}]",
                "authored map route has unresolved endpoint",
                parsed.source_row,
            )
        if all(ref in nodes for ref in endpoints):
            for side, endpoint, parent in (
                ("from", endpoints[0], parsed.from_subworld_ref),
                ("to", endpoints[1], parsed.to_subworld_ref),
            ):
                if parent and (
                    parent not in worlds
                    or worlds[parent] != node_worlds[nodes[endpoint]]
                ):
                    fail(
                        f"edges[{index}].{side}_subworld",
                        "route parent does not match node ownership",
                        parsed.source_row,
                    )


def with_genesis_map_identities(atlas: dict[str, Any]) -> dict[str, Any]:
    """Supply omitted IDs and expose existing node parent aliases to consumers.

    Only omitted identities are supplied. Duplicate authored IDs are rejected by
    validation; this helper never renames them or changes hierarchy references.
    """
    result = dict(atlas)
    for kind, prefix in (
        ("submaps", "subworld"),
        ("regions", "region"),
        ("nodes", "node"),
    ):
        rows = result.get(kind)
        if isinstance(rows, list):
            prepared = []
            for index, row in enumerate(rows, start=1):
                if isinstance(row, dict):
                    row = dict(row)
                    if isinstance(row.get("id", ""), str):
                        row["id"] = row.get("id", "").strip() or f"{prefix}-{index}"
                prepared.append(row)
            result[kind] = prepared
    if isinstance(result.get("nodes"), list):
        for node in result["nodes"]:
            if isinstance(node, dict) and node.get("parent_subworld", "") == "":
                for key in _NODE_PARENT_FIELDS[1:]:
                    if node.get(key):
                        node["parent_subworld"] = node[key]
                        break
    return result
