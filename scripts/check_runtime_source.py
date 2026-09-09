"""Reject new parallel production source/patch paths in a candidate Git diff.

Existing rollback artifacts may be deleted after verification, but are read-only
while retained. This is a release hygiene check, not a runtime security boundary.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


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
            replaces_source = re.search(r"\.replace\s*\(|\breplace_once\s*\(|\bsed\s+-i", contents)
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
