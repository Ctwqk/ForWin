"""The producer and consumers share one explicit Genesis route contract.

Legacy dictionaries remain immutable evidence. Their finite, existing aliases are
interpreted here, never separately by the preview and the BookMap importer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forwin.protocol.book_state import MapEdgeType
from forwin.utils.duration import duration_hours, is_non_exact_duration

from .visibility import genesis_edge_visibility


class GenesisRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    id: str = ""
    from_ref: str = Field(alias="from", min_length=1)
    to_ref: str = Field(alias="to", min_length=1)
    relation: str = ""
    edge_type: str = Field(
        default="path", json_schema_extra={"enum": [kind.value for kind in MapEdgeType]}
    )
    duration_text: str = Field(
        default="",
        description="Original travel duration with units; preserve ranges and uncertainty.",
    )
    mode: str = ""
    conditions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    bidirectional: bool = True
    status: Literal["open", "hidden", "blocked", "destroyed", "sealed", "closed"] = (
        "open"
    )
    visibility_default: Literal["visible", "hidden"] = "visible"
    discovered_by_default: bool = True
    access_rule_id: str = ""


class GenesisRouteContractError(ValueError):
    def __init__(self, path: str, message: str, raw_value: Any):
        self.path = path
        self.raw_value = deepcopy(raw_value)
        super().__init__(f"{path}: {message}")


_FROM = (
    "from_node_id",
    "source_node_id",
    "from_node",
    "source_node",
    "from",
    "source",
    "from_subworld_id",
    "source_subworld_id",
    "from_subworld",
    "source_subworld",
)
_TO = (
    "to_node_id",
    "target_node_id",
    "to_node",
    "target_node",
    "to",
    "target",
    "to_subworld_id",
    "target_subworld_id",
    "to_subworld",
    "target_subworld",
)
_LEGACY = set(_FROM + _TO) | {
    "edge_id",
    "source_edge_id",
    "kind",
    "type",
    "travel_cost",
    "travel_time",
    "constraints",
    "control",
    "access",
    "hazard",
    "risk",
    "hidden",
    "visibility",
}
_CANONICAL = {field.alias or name for name, field in GenesisRoute.model_fields.items()}


@dataclass(frozen=True)
class ParsedGenesisRoute:
    route: GenesisRoute
    hours: float | None
    source_path: str
    source_row: dict[str, Any]
    from_subworld_ref: str = ""
    to_subworld_ref: str = ""

    def source_id(self, index: int) -> str:
        return (
            self.route.id
            or f"route-{index}-{hashlib.sha256(json.dumps(self.source_row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]}"
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "source_travel_cost": self.route.duration_text,
            "travel_time_known": self.hours is not None,
            "source_relation": self.route.relation or self.route.edge_type,
            "source_mode": self.route.mode,
            "source_control": "；".join(self.route.conditions),
            "source_hazard": "；".join(self.route.risks),
            "source_route": deepcopy(self.source_row),
            "source_route_path": self.source_path,
        }


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value.strip()))


def _text_items(row: dict, fields: tuple[str, ...], path: str) -> list[str]:
    values = []
    for field in fields:
        if field not in row:
            continue
        raw = row[field]
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            values.extend(raw)
        else:
            raise GenesisRouteContractError(
                f"{path}.{field}", "expected text or a list of text", row
            )
    return _unique(values)


def _reference(
    row: dict, fields: tuple[str, ...], path: str, *, required: bool = True
) -> str:
    values = []
    for field in fields:
        raw = row.get(field)
        if isinstance(raw, dict):
            raw = (
                raw.get("id")
                or raw.get("node_id")
                or raw.get("name")
                or raw.get("subworld_id")
            )
        if raw in (None, ""):
            continue
        if not isinstance(raw, str):
            raise GenesisRouteContractError(
                f"{path}.{field}", "expected a reference string", row
            )
        values.append(raw.strip())
    values = _unique(values)
    if not values and not required:
        return ""
    if len(values) != 1:
        raise GenesisRouteContractError(
            path, "missing or conflicting route references", row
        )
    return values[0]


def _endpoint(row: dict, fields: tuple[str, ...], path: str) -> str:
    specific = _reference(row, fields[:6], path, required=False)
    parent = _reference(row, fields[6:], path + "_subworld", required=False)
    if not specific and not parent:
        raise GenesisRouteContractError(path, "missing route reference", row)
    return specific or parent


def _validate_legacy_scalars(row: dict, path: str) -> None:
    for field in (
        "id",
        "edge_id",
        "source_edge_id",
        "relation",
        "edge_type",
        "kind",
        "type",
        "mode",
        "access_rule_id",
        "status",
        "visibility",
        "visibility_default",
    ):
        if field in row and not isinstance(row[field], str):
            raise GenesisRouteContractError(f"{path}.{field}", "expected text", row)
    for field in ("hidden", "discovered_by_default", "bidirectional"):
        if field not in row:
            continue
        value = row[field]
        if type(value) is bool or type(value) is int and value in (0, 1):
            continue
        if isinstance(value, str) and value.lower() in {
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
        }:
            continue
        raise GenesisRouteContractError(
            f"{path}.{field}", "expected an explicit boolean", row
        )


def _duration(row: dict, path: str) -> tuple[str, float | None]:
    texts: list[str] = []
    candidates: list[tuple[str, float | None]] = []
    for field in ("duration_text", "travel_time", "travel_cost"):
        if field not in row:
            continue
        value = row[field]
        if field == "travel_time" and type(value) in (int, float):
            if not math.isfinite(value) or value < 0:
                raise GenesisRouteContractError(
                    f"{path}.{field}", "hours must be finite and nonnegative", row
                )
            text, hours = f"{value:.15g}小时", float(value)
        elif isinstance(value, str):
            if not value.strip():
                continue
            text = value
            hours = duration_hours(re.split(r"[；;]", value, maxsplit=1)[0])
        else:
            raise GenesisRouteContractError(
                f"{path}.{field}",
                "expected duration text or numeric travel_time in hours",
                row,
            )
        texts.append(text)
        # travel_cost is also historical fee/procedure prose. Only an explicit
        # duration in it supplies a numerical assertion; always retain the prose.
        if (
            field != "travel_cost"
            or hours is not None
            or is_non_exact_duration(re.split(r"[；;]", text, maxsplit=1)[0])
        ):
            candidates.append((text, hours))
    candidates = list(dict.fromkeys(candidates))
    known = [hours for _, hours in candidates if hours is not None]
    if (len(candidates) > 1 and any(hours is None for _, hours in candidates)) or (
        known
        and any(
            not math.isclose(hours, known[0], rel_tol=1e-9, abs_tol=1e-12)
            for hours in known
        )
    ):
        raise GenesisRouteContractError(
            path, "conflicting or incomparable duration sources", row
        )
    return "；".join(_unique(texts)), known[0] if known else None


def parse_genesis_route(
    row: Any, *, path: str, canonical: bool = False
) -> ParsedGenesisRoute:
    if not isinstance(row, dict):
        raise GenesisRouteContractError(path, "expected a route object", row)
    unsupported = set(row) - (_CANONICAL if canonical else _CANONICAL | _LEGACY)
    if unsupported:
        raise GenesisRouteContractError(
            path, "unsupported route fields: " + ", ".join(sorted(unsupported)), row
        )
    if not canonical:
        _validate_legacy_scalars(row, path)
    duration_text, hours = _duration(row, path)
    legacy_kind = row.get("kind") or row.get("type") or "path"
    values = (
        dict(row)
        if canonical
        else {
            "id": row.get("id")
            or row.get("edge_id")
            or row.get("source_edge_id")
            or "",
            "from": _endpoint(row, _FROM, path + ".from"),
            "to": _endpoint(row, _TO, path + ".to"),
            "relation": row.get("relation") or "",
            # Historical kind/type may be descriptive prose (for example sea_lane).
            # Only an explicit edge_type asserts a BookMap enum; keep prose in source_route.
            "edge_type": row.get("edge_type")
            or (
                legacy_kind
                if legacy_kind in {kind.value for kind in MapEdgeType}
                else "path"
            ),
            "mode": row.get("mode", ""),
            "conditions": _text_items(
                row, ("conditions", "constraints", "control", "access"), path
            ),
            "risks": _text_items(row, ("risks", "hazard", "risk"), path),
            "bidirectional": str(row.get("bidirectional", True)).lower()
            not in {"false", "0", "no"},
            "access_rule_id": row.get("access_rule_id") or "",
            **genesis_edge_visibility(row),
        }
    )
    values["duration_text"] = duration_text
    try:
        route = GenesisRoute.model_validate(values)
    except ValidationError as exc:
        locations = [
            ".".join(str(part) for part in error["loc"]) for error in exc.errors()
        ]
        raise GenesisRouteContractError(
            path, "invalid route fields: " + ", ".join(locations), row
        ) from exc
    if route.edge_type not in {kind.value for kind in MapEdgeType}:
        raise GenesisRouteContractError(path + ".edge_type", "unknown edge type", row)
    return ParsedGenesisRoute(
        route,
        hours,
        path,
        deepcopy(row),
        _reference(row, _FROM[6:], path + ".from_subworld", required=False),
        _reference(row, _TO[6:], path + ".to_subworld", required=False),
    )


def parse_genesis_routes(
    rows: Any, *, path: str = "world.map_atlas.edges", canonical: bool = False
) -> list[ParsedGenesisRoute]:
    if not isinstance(rows, list):
        raise GenesisRouteContractError(path, "expected a route list", rows)
    routes = [
        parse_genesis_route(row, path=f"{path}[{index}]", canonical=canonical)
        for index, row in enumerate(rows)
    ]
    seen = set()
    for index, parsed in enumerate(routes):
        identity = parsed.source_id(index)
        if identity in seen:
            raise GenesisRouteContractError(
                parsed.source_path, "duplicate authored map edge", parsed.source_row
            )
        seen.add(identity)
    return routes


def genesis_route_prompt_contract() -> str:
    return (
        "每条新路线使用唯一字段契约：from/to 为既有地点或 SubWorld 引用，id 保持稳定，"
        "relation 为关系说明，edge_type 为 BookMap 路线类型（不确定时 path），bidirectional 为布尔值。"
        "duration_text 保存带单位的完整耗时原文，范围及不确定条件不改成单一数值；mode 保存交通方式，"
        "conditions 和 risks 分别为通行条件与风险的字符串数组。"
        "access_rule_id 只引用已有规则，不能用许可说明冒充 ID 或已获授权。"
        "status、visibility_default、discovered_by_default 保留封闭/隐藏/未发现边界。"
        "新路线不使用自由字段或另造同义字段；旧修订中未涉及的原始字段保持不变，系统会统一验证其合同。"
    )
