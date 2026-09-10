"""Reject new parallel production source/patch paths in a candidate Git diff.

Existing rollback artifacts may be deleted after verification, but are read-only
while retained. This is a release hygiene check, not a runtime security boundary.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
from pathlib import Path


def _without_data_file_replacements(contents: str) -> str:
    """Distinguish Python file renames from text replacement without a file exemption."""
    try:
        tree = ast.parse(contents)
    except SyntaxError:
        return contents
    assignments: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assignments.setdefault(target.id, []).append(value)
    encoded = contents.encode("utf-8")
    lines = encoded.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    masked = bytearray(encoded)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            continue
        destinations = list(node.args[1:2]) + [
            keyword.value for keyword in node.keywords if keyword.arg == "dst"
        ]
        if not destinations:
            continue
        # Resolve simple static targets, conservatively across scopes. This is
        # a hygiene check, not a general Python data-flow or security analysis.
        pending = list(destinations)
        seen_names: set[str] = set()
        while pending:
            expression = pending.pop()
            for child in ast.walk(expression):
                if isinstance(child, ast.Name) and child.id not in seen_names:
                    seen_names.add(child.id)
                    values = assignments.get(child.id, [])
                    destinations.extend(values)
                    pending.extend(values)
        literals = " ".join(
            child.value
            for destination in destinations
            for child in ast.walk(destination)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
        if "/app" in literals and "forwin" in literals:
            continue  # Explicit atomic source overwrite is still a runtime patch.
        function = node.func
        start = offsets[function.lineno - 1] + function.col_offset
        end = offsets[function.end_lineno - 1] + function.end_col_offset
        masked[start:end] = b" " * (end - start)
    return masked.decode("utf-8")


def violations(repo: Path, base: str) -> list[str]:
    changed = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--name-only", "-z",
         "--diff-filter=ACMT", base, "--"],
    ).decode().split("\0")
    errors: list[str] = []
    for name in filter(None, changed):
        relative = Path(name)
        if relative.parts[0] in {"tests", "docs", "Design-docs"}:
            continue
        if name.startswith("deploy/forwin-runtime-hotfixes/"):
            errors.append(f"{name}: retained rollback artifacts are read-only")
            continue
        if "forwin" in relative.parts[:-1] and relative.parts[0] != "forwin":
            errors.append(f"{name}: production source belongs in the root forwin tree")
            continue
        path = repo / relative
        if not path.is_file():
            continue
        if "Dockerfile" in relative.name:
            contents = path.read_text()
            if re.search(r"(?im)^\s*FROM\s+\S*:compat[-:]", contents):
                errors.append(f"{name}: normal images cannot inherit a compat image")
            if re.search(r"(?im)^\s*(?:COPY|ADD|RUN)\b[^\n]*(?:runtime-hotfix|runtime-files|apply_runtime_hotfix)", contents):
                errors.append(f"{name}: normal builds cannot assemble runtime patches")
        elif relative.parts[0] in {"deploy", "scripts"} and relative.suffix in {".py", ".sh"}:
            if name == "scripts/check_runtime_source.py":
                continue
            contents = path.read_text()
            writes_runtime = "/app" in contents and "forwin" in contents
            inspected = (
                _without_data_file_replacements(contents)
                if relative.suffix == ".py"
                else contents
            )
            replaces_source = re.search(r"\.replace\s*\(|\breplace_once\s*\(|\bsed\s+-i", inspected)
            if writes_runtime and replaces_source:
                errors.append(f"{name}: runtime source substitution is not a release path")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True, help="Reviewed baseline Git revision")
    args = parser.parse_args()
    try:
        errors = violations(args.repo.resolve(), args.base)
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Cannot inspect candidate source: {exc}\n")
    for error in errors:
        print(error)
    if not errors:
        print("Runtime source policy passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
