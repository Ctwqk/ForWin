from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from forwin.config import InfrastructureConfig

from .cases import load_eval_cases, load_sample_contexts
from .profiles import load_eval_profiles, profile_requires_api_key
from .runner import EvalRunConfig, LLMReliabilityRunner


def run_eval_from_args(args) -> int:  # noqa: ANN001
    config = InfrastructureConfig.from_env()
    live = os.environ.get("FORWIN_LLM_EVAL_LIVE", "").strip().lower() in {"1", "true", "yes"}
    if not live and not getattr(args, "dry_run", False):
        print("Refusing to run live LLM eval. Set FORWIN_LLM_EVAL_LIVE=1 or pass --dry-run.")
        return 2
    run_id = str(getattr(args, "run_id", "") or "").strip() or f"llm-eval-{uuid.uuid4().hex[:12]}"
    profiles = load_eval_profiles(
        manifest_path=str(getattr(args, "manifest", "") or ""),
        selected_ids=str(getattr(args, "profiles", "") or ""),
    )
    if not profiles:
        print("No eval profiles found.")
        return 2
    missing_keys = [profile.id for profile in profiles if profile_requires_api_key(profile) and not profile.api_key]
    if missing_keys and not getattr(args, "dry_run", False):
        print(f"Profiles missing API keys: {', '.join(missing_keys)}")
        return 2

    suite = str(getattr(args, "suite", "medium") or "medium")
    try:
        contexts = load_sample_contexts(Path(args.context_file))
        cases = load_eval_cases(Path(args.cases_file), contexts=contexts, suite=suite)
    except (OSError, ValueError) as exc:
        print(f"Failed to load eval fixtures: {exc}")
        return 2
    if getattr(args, "dry_run", False):
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "profiles": [profile.id for profile in profiles],
                    "case_count": len(cases),
                    "cases": [case.case_id for case in cases],
                    "rounds": int(getattr(args, "rounds", 0) or (1 if str(getattr(args, "suite", "medium")) == "smoke" else 20)),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    runner = LLMReliabilityRunner(
        EvalRunConfig(
            run_id=run_id,
            artifact_root=str(getattr(args, "artifact_root", "") or config.artifact_root),
            suite=str(getattr(args, "suite", "medium") or "medium"),
            live=True,
            include_mini_real_run=not bool(getattr(args, "skip_mini_real_run", False)),
            mini_premise=str(getattr(args, "mini_premise", "") or ""),
            mini_genre=str(getattr(args, "mini_genre", "") or ""),
            rounds=int(getattr(args, "rounds", 0) or (1 if str(getattr(args, "suite", "medium")) == "smoke" else 20)),
        )
    )
    summary = runner.run(profiles=profiles, cases=cases)
    print(f"LLM eval complete: {summary['run_dir']}")
    return 0


def report_eval_from_args(args) -> int:  # noqa: ANN001
    config = InfrastructureConfig.from_env()
    root = Path(str(getattr(args, "artifact_root", "") or config.artifact_root))
    run_id = str(getattr(args, "run_id", "") or "").strip()
    if not run_id:
        print("--run-id is required")
        return 2
    run_dir = root / "llm_eval" / "runs" / run_id
    summary_md = run_dir / "summary.md"
    summary_json = run_dir / "summary.json"
    if summary_md.is_file():
        print(summary_md.read_text(encoding="utf-8"))
        return 0
    if summary_json.is_file():
        print(json.dumps(json.loads(summary_json.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
        return 0
    print(f"Run not found: {run_dir}")
    return 2
