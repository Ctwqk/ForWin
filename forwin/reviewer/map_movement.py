from __future__ import annotations

import re
from typing import Any

from forwin.book_state.cognition import CognitionView
from forwin.map.pathfinding import MapGraph
from forwin.protocol.book_state import CognitionOverlay, MapEdge, MapNode
from forwin.protocol.context import ChapterContextPack, ReviewContextPack
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput


def _duration_to_travel_time_budget(text: str) -> float | None:
    normalized = str(text or "").strip()
    if not normalized:
        return None
    if "一炷香" in normalized:
        return 0.5
    if "片刻" in normalized or "须臾" in normalized:
        return 0.25
    if "半个时辰" in normalized:
        return 1.0
    if "时辰" in normalized:
        return _extract_duration_number(normalized, default=1.0) * 2.0
    if "小时" in normalized:
        return _extract_duration_number(normalized, default=1.0)
    if "半日" in normalized or "半天" in normalized:
        return 12.0
    if any(token in normalized for token in ("次日", "翌日", "第二天", "一天", "一日")):
        return 24.0
    if "天" in normalized or "日" in normalized:
        return _extract_duration_number(normalized, default=1.0) * 24.0
    return None


def _extract_duration_number(text: str, *, default: float) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1))
    chinese_numbers = {
        "一": 1.0,
        "二": 2.0,
        "两": 2.0,
        "三": 3.0,
        "四": 4.0,
        "五": 5.0,
        "六": 6.0,
        "七": 7.0,
        "八": 8.0,
        "九": 9.0,
        "十": 10.0,
    }
    for key, value in chinese_numbers.items():
        if key in text:
            return value
    return default


def _edge_from_path_id(graph: MapGraph, edge_id: str) -> MapEdge | None:
    base_edge_id = str(edge_id or "").removesuffix("__reverse")
    return graph.edges_by_id.get(str(edge_id or "")) or graph.edges_by_id.get(base_edge_id)


def _float_policy_value(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class MapMovementReviewer:
    name = "map_movement"

    def review(
        self,
        context: ReviewContextPack | ChapterContextPack,
        writer_output: WriterOutput,
        **_kwargs,
    ) -> ReviewVerdict:
        issue = self.first_issue(context, writer_output)
        if issue is None:
            return ReviewVerdict(verdict="pass", issues=[])
        return ReviewVerdict(
            verdict="fail" if issue.severity == "error" else "warn",
            issues=[issue],
            recommended_action="rewrite" if issue.severity == "error" else "pause_for_review",
            reviewer_mode="heuristic_fallback",
            evidence_refs=list(issue.evidence_refs),
        )

    def first_issue(
        self,
        context: ReviewContextPack | ChapterContextPack,
        writer_output: WriterOutput,
    ) -> ContinuityIssue | None:
        if len(writer_output.scene_outputs) < 2:
            return None
        map_context = context.map_context or {}
        review_graph = map_context.get("review_graph") if isinstance(map_context.get("review_graph"), dict) else {}
        node_payloads = review_graph.get("map_nodes") if isinstance(review_graph.get("map_nodes"), list) else []
        edge_payloads = review_graph.get("map_edges") if isinstance(review_graph.get("map_edges"), list) else []
        if review_graph and review_graph.get("available") is False and (not node_payloads or not edge_payloads):
            return None
        if not node_payloads and not edge_payloads:
            node_payloads = map_context.get("map_nodes") if isinstance(map_context.get("map_nodes"), list) else []
            edge_payloads = map_context.get("map_edges") if isinstance(map_context.get("map_edges"), list) else []
        if not node_payloads or not edge_payloads:
            return None
        try:
            nodes = [MapNode.model_validate(payload) for payload in node_payloads if isinstance(payload, dict)]
            edges = [MapEdge.model_validate(payload) for payload in edge_payloads if isinstance(payload, dict)]
        except Exception:
            return None
        objective_graph_payload = (
            map_context.get("objective_review_graph")
            if isinstance(map_context.get("objective_review_graph"), dict)
            else {}
        )
        objective_nodes = nodes
        objective_edges = edges
        if objective_graph_payload:
            objective_node_payloads = (
                objective_graph_payload.get("map_nodes")
                if isinstance(objective_graph_payload.get("map_nodes"), list)
                else []
            )
            objective_edge_payloads = (
                objective_graph_payload.get("map_edges")
                if isinstance(objective_graph_payload.get("map_edges"), list)
                else []
            )
            try:
                parsed_objective_nodes = [
                    MapNode.model_validate(payload)
                    for payload in objective_node_payloads
                    if isinstance(payload, dict)
                ]
                parsed_objective_edges = [
                    MapEdge.model_validate(payload)
                    for payload in objective_edge_payloads
                    if isinstance(payload, dict)
                ]
            except Exception:
                parsed_objective_nodes = []
                parsed_objective_edges = []
            if parsed_objective_nodes and parsed_objective_edges:
                objective_nodes = parsed_objective_nodes
                objective_edges = parsed_objective_edges
        node_by_id = {node.id: node for node in nodes}
        node_id_by_name = {node.name: node.id for node in nodes if node.name}
        graph = MapGraph(nodes=nodes, edges=edges)
        objective_graph = MapGraph(nodes=objective_nodes, edges=objective_edges)
        cognition_by_observer = self._observer_cognition_views(map_context)
        movement_policy = self._movement_policy(map_context)

        ordered_scenes = sorted(writer_output.scene_outputs, key=lambda scene: scene.scene_no)
        path_refs: list[str] = []
        total_travel_time = 0.0
        used_observer_known_path = False
        for previous, current in zip(ordered_scenes, ordered_scenes[1:]):
            previous_id = self._resolve_scene_location_id(previous.scene_location_id, node_by_id, node_id_by_name)
            current_id = self._resolve_scene_location_id(current.scene_location_id, node_by_id, node_id_by_name)
            if not previous_id or not current_id or previous_id == current_id:
                continue
            observer = self._scene_observer(previous, current, map_context)
            objective_result = objective_graph.shortest_path(previous_id, current_id, metric="travel_time")
            known_result = None
            cognition = cognition_by_observer.get(observer) if observer is not None else None
            if cognition is not None:
                known_graph = MapGraph(
                    nodes=objective_nodes,
                    edges=objective_edges,
                    cognition_by_observer={observer: cognition},
                )
                known_result = known_graph.shortest_path(
                    previous_id,
                    current_id,
                    metric="travel_time",
                    observer=observer,
                )
                used_observer_known_path = True
            result = known_result or graph.shortest_path(previous_id, current_id, metric="travel_time")
            if not objective_result.reachable:
                blocked_result = objective_graph.shortest_path(
                    previous_id,
                    current_id,
                    metric="travel_time",
                    allow_blocked=True,
                )
                if blocked_result.reachable:
                    return self._map_path_issue(
                        rule_name="map_blocked_route_unusable",
                        description="相邻场景依赖的地图路线存在，但当前处于 blocked/sealed/destroyed 状态。",
                        previous_scene_no=previous.scene_no,
                        current_scene_no=current.scene_no,
                        previous_id=previous_id,
                        current_id=current_id,
                        evidence_refs=[
                            f"scene:{previous.scene_no}->{current.scene_no}",
                            f"from={previous_id}",
                            f"to={current_id}",
                            f"blocked_path={','.join(blocked_result.path_edge_ids)}",
                        ],
                        suggested_fix="补出解封/绕行过程，或调整为当前可通行路线。",
                    )
                return self._map_path_issue(
                    rule_name="map_path_unreachable",
                    description="相邻场景发生地点切换，但 objective 地图图中没有可达路线。",
                    previous_scene_no=previous.scene_no,
                    current_scene_no=current.scene_no,
                    previous_id=previous_id,
                    current_id=current_id,
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"from={previous_id}",
                        f"to={current_id}",
                        f"blocked_reason={objective_result.blocked_reason}",
                    ],
                    suggested_fix="调整场景地点、补充合理赶路过程，或在地图中添加 objective 可达路线。",
                )
            if known_result is not None and not known_result.reachable:
                hidden_unseen = self._path_uses_hidden_edge(objective_result.path_edge_ids, objective_graph, cognition)
                return self._map_path_issue(
                    rule_name="map_hidden_route_unseen" if hidden_unseen else "map_known_path_unreachable",
                    description=(
                        "objective 路线可达，但当前观察者尚不知道关键隐藏路线。"
                        if hidden_unseen
                        else "objective 路线可达，但当前观察者认知图中没有可达路线。"
                    ),
                    previous_scene_no=previous.scene_no,
                    current_scene_no=current.scene_no,
                    previous_id=previous_id,
                    current_id=current_id,
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"observer={observer[0]}:{observer[1]}",
                        f"objective_path={','.join(objective_result.path_edge_ids)}",
                        f"known_blocked_reason={known_result.blocked_reason}",
                    ],
                    suggested_fix="让角色先发现/确认路线，改走已知路线，或补出误入隐藏路线的叙事因果。",
                )
            if known_result is not None and self._path_uses_false_edge(known_result.path_edge_ids, cognition):
                return self._map_path_issue(
                    rule_name="map_false_route_used",
                    description="章节移动采用了观察者认知中的 false route，缺少被骗或修正的叙事处理。",
                    previous_scene_no=previous.scene_no,
                    current_scene_no=current.scene_no,
                    previous_id=previous_id,
                    current_id=current_id,
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"observer={observer[0]}:{observer[1]}",
                        f"known_path={','.join(known_result.path_edge_ids)}",
                    ],
                    suggested_fix="改为真实可达路线，或把 false route 写成误导并付出绕行/失败代价。",
                )
            if not result.reachable:
                return ContinuityIssue(
                    rule_name="map_path_unreachable",
                    severity="error",
                    description="相邻场景发生地点切换，但地图图中没有可达路线。",
                    reviewer=self.name,
                    issue_type="continuity",
                    target_scope="scene",
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"from={previous_id}",
                        f"to={current_id}",
                        f"blocked_reason={result.blocked_reason}",
                    ],
                    suggested_fix="调整场景地点、补充合理赶路过程，或在地图中添加可达路线。",
                )
            unmet_access_rule = self._path_unmet_access_rule(
                result.path_edge_ids,
                objective_graph,
                movement_policy,
            )
            if unmet_access_rule is not None:
                edge_id, access_rule_id = unmet_access_rule
                return self._map_path_issue(
                    rule_name="map_access_rule_unmet",
                    description="相邻场景使用了当前 movement policy 未授权的地图通行规则。",
                    previous_scene_no=previous.scene_no,
                    current_scene_no=current.scene_no,
                    previous_id=previous_id,
                    current_id=current_id,
                    evidence_refs=[
                        f"scene:{previous.scene_no}->{current.scene_no}",
                        f"from={previous_id}",
                        f"to={current_id}",
                        f"edge_id={edge_id}",
                        f"access_rule_id={access_rule_id}",
                        f"path={','.join(result.path_edge_ids)}",
                    ],
                    suggested_fix="补出通行凭证/权限获取，改走已授权路线，或更新 reviewer-only movement policy。",
                )
            effective_travel_time = self._effective_path_travel_time(result, objective_graph, movement_policy)
            total_travel_time += effective_travel_time
            path_refs.append(
                f"scene:{previous.scene_no}->{current.scene_no}:travel_time={round(effective_travel_time, 3)}:raw_travel_time={result.total_travel_time}:path={','.join(result.path_edge_ids)}"
            )

        if total_travel_time <= 0:
            return None
        budget = self._chapter_travel_time_budget(context, writer_output)
        if budget is None or total_travel_time <= budget:
            return None
        return ContinuityIssue(
            rule_name=(
                "map_known_travel_time_exceeds_chapter_time"
                if used_observer_known_path
                else "map_travel_time_exceeds_chapter_time"
            ),
            severity="error",
            description=(
                "角色按 observer-known 路径移动所需赶路时间超过本章时间推进。"
                if used_observer_known_path
                else "角色场景移动所需地图赶路时间超过本章时间推进。"
            ),
            reviewer=self.name,
            issue_type="continuity",
            target_scope="scene",
            evidence_refs=[
                f"required_travel_time={round(total_travel_time, 3)}",
                f"available_time={round(budget, 3)}",
                *path_refs[:4],
            ],
            suggested_fix="延长章节时间推进、改为更近地点、使用已知快速路线，或补出合理中转。",
        )

    @staticmethod
    def _observer_cognition_views(map_context: dict) -> dict[tuple[str, str], CognitionView]:
        payload = map_context.get("observer_cognition")
        if not isinstance(payload, dict):
            return {}
        views: dict[tuple[str, str], CognitionView] = {}
        for value in payload.values():
            if not isinstance(value, dict):
                continue
            try:
                overlay = CognitionOverlay.model_validate(value)
            except Exception:
                continue
            observer_key = (str(overlay.observer_type), overlay.observer_id)
            views[observer_key] = CognitionView(overlay)
        return views

    @staticmethod
    def _scene_observer(previous_scene, current_scene, map_context: dict) -> tuple[str, str] | None:
        active_locations = [
            item
            for item in map_context.get("active_locations", [])
            if isinstance(item, dict)
        ]
        if not active_locations:
            return None
        involved = [
            str(item).strip()
            for item in [
                *list(getattr(previous_scene, "involved_entities", []) or []),
                *list(getattr(current_scene, "involved_entities", []) or []),
            ]
            if str(item).strip()
        ]
        for entity_ref in involved:
            for active in active_locations:
                entity_id = str(active.get("entity_id", "") or "").strip()
                entity_name = str(active.get("entity_name", "") or "").strip()
                if entity_ref and (entity_ref == entity_id or entity_ref == entity_name):
                    return ("character", entity_id or entity_ref)
        return None

    @staticmethod
    def _path_uses_hidden_edge(
        edge_ids: list[str],
        graph: MapGraph,
        cognition: CognitionView | None,
    ) -> bool:
        hidden_refs = cognition.hidden_refs if cognition is not None else set()
        for edge_id in edge_ids:
            base_edge_id = edge_id.removesuffix("__reverse")
            edge = graph.edges_by_id.get(edge_id) or graph.edges_by_id.get(base_edge_id)
            if edge is None:
                continue
            edge_ref = f"map_edge:{base_edge_id}"
            if edge_ref in hidden_refs:
                return True
            if edge.status == "hidden" or edge.edge_type == "hidden_route" or not edge.discovered_by_default:
                return True
        return False

    @staticmethod
    def _path_uses_false_edge(edge_ids: list[str], cognition: CognitionView | None) -> bool:
        if cognition is None:
            return False
        false_ids: set[str] = set()
        for key, value in cognition.false_edges.items():
            false_ids.add(str(key))
            false_ids.add(str(key).removeprefix("map_edge:"))
            if isinstance(value, MapEdge):
                false_ids.add(value.id)
        return any(edge_id in false_ids or edge_id.removesuffix("__reverse") in false_ids for edge_id in edge_ids)

    @staticmethod
    def _movement_policy(map_context: dict) -> dict[str, Any]:
        policy = map_context.get("movement_policy")
        return dict(policy) if isinstance(policy, dict) else {}

    @staticmethod
    def _path_unmet_access_rule(
        edge_ids: list[str],
        graph: MapGraph,
        movement_policy: dict[str, Any],
    ) -> tuple[str, str] | None:
        if "allowed_access_rule_ids" not in movement_policy:
            return None
        raw_allowed = movement_policy.get("allowed_access_rule_ids")
        allowed = {
            str(item).strip()
            for item in (raw_allowed if isinstance(raw_allowed, list) else [])
            if str(item).strip()
        }
        for edge_id in edge_ids:
            edge = _edge_from_path_id(graph, edge_id)
            if edge is None:
                continue
            access_rule_id = str(edge.access_rule_id or "").strip()
            if access_rule_id and access_rule_id not in allowed:
                return edge.id, access_rule_id
        return None

    @staticmethod
    def _effective_path_travel_time(
        result,
        graph: MapGraph,
        movement_policy: dict[str, Any],
    ) -> float:
        edge_type_multipliers = movement_policy.get("travel_time_multiplier_by_edge_type")
        multipliers = edge_type_multipliers if isinstance(edge_type_multipliers, dict) else {}
        total = 0.0
        used_edges = False
        for edge_id in result.path_edge_ids:
            edge = _edge_from_path_id(graph, edge_id)
            if edge is None:
                continue
            used_edges = True
            multiplier = _float_policy_value(multipliers.get(str(edge.edge_type)), default=1.0)
            total += float(edge.travel_time or 0.0) * multiplier
        if not used_edges:
            total = float(result.total_travel_time or 0.0)
        team_speed_multiplier = _float_policy_value(movement_policy.get("team_speed_multiplier"), default=1.0)
        if team_speed_multiplier <= 0:
            team_speed_multiplier = 1.0
        return total / team_speed_multiplier

    def _map_path_issue(
        self,
        *,
        rule_name: str,
        description: str,
        previous_scene_no: int,
        current_scene_no: int,
        previous_id: str,
        current_id: str,
        evidence_refs: list[str],
        suggested_fix: str,
    ) -> ContinuityIssue:
        return ContinuityIssue(
            rule_name=rule_name,
            severity="error",
            description=description,
            reviewer=self.name,
            issue_type="continuity",
            target_scope="scene",
            evidence_refs=evidence_refs
            or [
                f"scene:{previous_scene_no}->{current_scene_no}",
                f"from={previous_id}",
                f"to={current_id}",
            ],
            suggested_fix=suggested_fix,
        )

    @staticmethod
    def _resolve_scene_location_id(
        raw_location: str,
        node_by_id: dict[str, MapNode],
        node_id_by_name: dict[str, str],
    ) -> str:
        text = str(raw_location or "").strip()
        if not text:
            return ""
        if text in node_by_id:
            return text
        return node_id_by_name.get(text, "")

    @staticmethod
    def _chapter_travel_time_budget(
        context: ReviewContextPack | ChapterContextPack,
        writer_output: WriterOutput,
    ) -> float | None:
        map_context = context.map_context or {}
        explicit_budget = map_context.get("chapter_travel_time_budget")
        if explicit_budget is not None:
            try:
                return max(0.0, float(explicit_budget))
            except (TypeError, ValueError):
                pass
        if writer_output.time_advance is None:
            return None
        return _duration_to_travel_time_budget(writer_output.time_advance.duration_description)
