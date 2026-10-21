"""Load LLM-eval contexts and cases from fixture files.

This module owns structure only: how a context file and a cases file are read and how
prompt builders are bound to their arguments. Every piece of story text (names, premises,
prompt wording) lives in the fixture files handed to the loaders, never in this package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from forwin.protocol.context import ChapterContextPack
from forwin.writer.prompt_core import (
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
)

from .schemas import EvalCase


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_sample_contexts(path: Path) -> list[ChapterContextPack]:
    """Read a JSON list of serialized ``ChapterContextPack`` objects."""
    payload = _read_json(path)
    if not isinstance(payload, list):
        raise ValueError(f"{path}: expected a JSON list of chapter contexts")
    contexts = [ChapterContextPack.model_validate(item) for item in payload]
    if not contexts:
        raise ValueError(f"{path}: no sample contexts found")
    return contexts


def _preview_chapter(context: ChapterContextPack, args: dict[str, Any]) -> list[dict[str, Any]]:
    return build_preview_chapter_prompt(
        context,
        target_chars=args["target_chars"],
        min_chars=args["min_chars"],
        max_chars=args["max_chars"],
    )


def _scene_breakdown(context: ChapterContextPack, args: dict[str, Any]) -> list[dict[str, Any]]:
    return build_scene_breakdown_prompt(
        context,
        default_scene_count=args["default_scene_count"],
        max_scene_count=args["max_scene_count"],
    )


_PROMPT_BUILDERS: dict[str, Callable[[ChapterContextPack, dict[str, Any]], list[dict[str, Any]]]] = {
    "preview_chapter": _preview_chapter,
    "scene_breakdown": _scene_breakdown,
}


def _build_case(
    raw: dict[str, Any],
    *,
    context: ChapterContextPack,
) -> EvalCase:
    spec = {key: value for key, value in raw.items() if key not in {"suites", "prompt"}}
    case_id = str(spec.get("case_id", "") or "<unnamed>")
    prompt = raw.get("prompt")
    if prompt is not None:
        if "messages" in spec:
            raise ValueError(f"case {case_id}: give either 'prompt' or 'messages', not both")
        builder_name = str(prompt.get("builder", ""))
        builder = _PROMPT_BUILDERS.get(builder_name)
        if builder is None:
            raise ValueError(
                f"case {case_id}: unknown prompt builder {builder_name!r}; "
                f"known builders: {', '.join(sorted(_PROMPT_BUILDERS))}"
            )
        try:
            spec["messages"] = builder(context, dict(prompt.get("args") or {}))
        except KeyError as exc:
            raise ValueError(
                f"case {case_id}: prompt builder {builder_name!r} is missing argument {exc.args[0]!r}"
            ) from exc
        spec["input_snapshot"] = {"chapter_number": context.chapter_number}
    return EvalCase.model_validate(spec)


def load_eval_cases(
    path: Path,
    *,
    contexts: list[ChapterContextPack],
    suite: str,
) -> list[EvalCase]:
    """Build the eval cases of ``suite`` from a cases file.

    A case entry carries either literal ``messages`` or a ``prompt`` naming one of the
    known prompt builders, which is run against the first sample context. An entry without
    ``suites`` belongs to every suite.
    """
    if not contexts:
        raise ValueError("load_eval_cases needs at least one sample context")
    payload = _read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError(f"{path}: expected a JSON object with a 'cases' list")
    cases: list[EvalCase] = []
    for raw in payload["cases"]:
        suites = raw.get("suites")
        if suites is not None and suite not in suites:
            continue
        cases.append(_build_case(raw, context=contexts[0]))
    return cases
