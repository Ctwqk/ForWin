from __future__ import annotations

import json
from pathlib import Path


def write_canvas(path: Path, *, page_paths: list[str], edges: list[tuple[str, str, str]]) -> None:
    nodes = []
    node_ids: dict[str, str] = {}
    for index, page_path in enumerate(page_paths):
        node_id = f"node_{index + 1}"
        node_ids[page_path] = node_id
        nodes.append(
            {
                "id": node_id,
                "type": "file",
                "file": page_path,
                "x": (index % 4) * 420,
                "y": (index // 4) * 260,
                "width": 360,
                "height": 220,
            }
        )
    canvas_edges = []
    for index, (source, target, label) in enumerate(edges):
        if source not in node_ids or target not in node_ids:
            continue
        canvas_edges.append(
            {
                "id": f"edge_{index + 1}",
                "fromNode": node_ids[source],
                "fromSide": "right",
                "toNode": node_ids[target],
                "toSide": "left",
                "label": label,
            }
        )
    path.write_text(
        json.dumps({"nodes": nodes, "edges": canvas_edges}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
