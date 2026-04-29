from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORWIN = ROOT / "forwin"


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in FORWIN.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _literal_arg(call: ast.Call, index: int, keyword: str) -> str:
    for item in call.keywords:
        if item.arg == keyword and isinstance(item.value, ast.Constant):
            return str(item.value.value)
    if index >= 0 and len(call.args) > index and isinstance(call.args[index], ast.Constant):
        return str(call.args[index].value)
    return ""


def test_character_entities_are_not_created_outside_character_creation_helper() -> None:
    allowed = {
        FORWIN / "characters" / "creation.py",
    }
    violations: list[str] = []
    for path in _python_files():
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "create_entity":
                continue
            if _literal_arg(node, index=1, keyword="kind") == "character":
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == []


def test_book_state_character_nodes_are_not_persisted_outside_character_creation_helper() -> None:
    allowed = {
        FORWIN / "characters" / "creation.py",
    }
    violations: list[str] = []
    for path in _python_files():
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "WorldNode":
                if _literal_arg(node, index=-1, keyword="node_type") == "character":
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == []
