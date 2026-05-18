from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forwin.config import Config
from forwin.canon_quality.chapter_review_form.service import load_form_artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read a persisted chapter review form artifact.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--chapter-number", required=True, type=int)
    args = parser.parse_args(argv)

    config = Config.from_env()
    try:
        artifact = load_form_artifact(config.artifact_root, args.project_id, args.chapter_number)
    except FileNotFoundError:
        print(
            f"chapter review form artifact not found for project={args.project_id} chapter={args.chapter_number}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
