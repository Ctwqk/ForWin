#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forwin.writer.llm_client import LLMClient


DEFAULT_STAGES = (
    "launch_arc_",
    "arc_plan",
    "arc_envelope_analysis",
    "scene_breakdown",
    "scene_generation",
    "scene_stitch",
    "subworld_delta",
    "npc_intents",
    "world_pressure",
    "state_event_extraction",
    "thread_time_extraction",
    "lore_timeline_notes_extraction",
)


@dataclass
class ProbeCase:
    name: str
    messages: list[dict[str, Any]]
    temperature: float = 0.7
    max_tokens: int = 900
    source_path: str = ""
    stage_key: str = ""


@dataclass
class ProbeAttempt:
    case_name: str
    model: str
    success: bool
    status_code: int = 0
    duration_ms: int = 0
    output_chars: int = 0
    error_category: str = ""
    error_class: str = ""
    error_message: str = ""
    response_preview: str = ""
    prompt_chars: int = 0
    source_path: str = ""
    started_at: str = ""
    completed_at: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _rate(successes: int, total: int) -> float:
    return successes / total if total else 0.0


def _bucket_summary(attempts: list[ProbeAttempt]) -> dict[str, Any]:
    total = len(attempts)
    successes = sum(1 for attempt in attempts if attempt.success)
    failures = total - successes
    durations = [attempt.duration_ms for attempt in attempts if attempt.duration_ms >= 0]
    return {
        "attempts": total,
        "successes": successes,
        "failures": failures,
        "success_rate": _rate(successes, total),
        "avg_duration_ms": int(sum(durations) / len(durations)) if durations else 0,
        "status_codes": _count_by(attempts, "status_code"),
    }


def _count_by(attempts: list[ProbeAttempt], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        value = getattr(attempt, field)
        if value in ("", 0):
            continue
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def summarize_attempts(attempts: list[ProbeAttempt]) -> dict[str, Any]:
    total = len(attempts)
    successes = sum(1 for attempt in attempts if attempt.success)
    by_case: dict[str, list[ProbeAttempt]] = {}
    by_model: dict[str, list[ProbeAttempt]] = {}
    for attempt in attempts:
        by_case.setdefault(attempt.case_name, []).append(attempt)
        by_model.setdefault(attempt.model, []).append(attempt)
    return {
        "generated_at": utc_now(),
        "total_attempts": total,
        "successes": successes,
        "failures": total - successes,
        "success_rate": _rate(successes, total),
        "by_case": {
            key: _bucket_summary(value)
            for key, value in sorted(by_case.items())
        },
        "by_model": {
            key: _bucket_summary(value)
            for key, value in sorted(by_model.items())
        },
        "error_categories": _count_by(attempts, "error_category"),
    }


def builtin_non_code_cases() -> list[ProbeCase]:
    return [
        ProbeCase(
            name="noncode:zh_summary",
            messages=[
                {"role": "system", "content": "你是中文长文摘要助手。"},
                {
                    "role": "user",
                    "content": (
                        "请把下面这段叙事材料整理成三段：人物关系、世界规则、后续悬念。\n\n"
                        "海湾城在一次异常低潮后露出地下回廊，档案员林澈和潜水工程师许遥"
                        "沿着潮汐门进入旧城防灾系统，发现父亲失踪记录被人为改写。"
                    ),
                },
            ],
            temperature=0.4,
            max_tokens=700,
            stage_key="summary",
        ),
        ProbeCase(
            name="noncode:editorial_review",
            messages=[
                {"role": "system", "content": "你是严谨的小说编辑，只给结构建议。"},
                {
                    "role": "user",
                    "content": (
                        "请评估这章是否有足够的情绪递进：主角刚发现盟友隐瞒了地图来源，"
                        "但仍必须和对方一起穿过废弃生态舱。输出 JSON，字段为 strengths、risks、revision_notes。"
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=800,
            stage_key="scene_breakdown",
        ),
        ProbeCase(
            name="noncode:worldbuilding",
            messages=[
                {"role": "system", "content": "你是世界观设定助手，只输出 JSON。"},
                {
                    "role": "user",
                    "content": (
                        "为一个环形星门城市设计三个非政府组织。每个组织需要 name、goal、leverage、"
                        "relationship_to_protagonist，内容必须是科幻悬疑题材。"
                    ),
                },
            ],
            temperature=0.5,
            max_tokens=900,
            stage_key="arc_plan",
        ),
    ]


def _run_docker(container: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _stage_from_path(path: str) -> str:
    parts = path.split("/")
    if "llm_traces" not in parts:
        return ""
    index = parts.index("llm_traces")
    if index + 2 >= len(parts):
        return ""
    return parts[index + 2]


def _history_path_allowed(path: str, stage_filters: tuple[str, ...]) -> bool:
    stage = _stage_from_path(path)
    return any(stage == item or stage.startswith(item) for item in stage_filters)


def load_forwin_history_cases(
    *,
    container: str,
    root: str,
    limit: int,
    stage_filters: tuple[str, ...],
) -> list[ProbeCase]:
    proc = _run_docker(
        container,
        "find",
        root,
        "-path",
        "*llm_traces*",
        "-name",
        "*_raw_prompt.json",
        timeout=120,
    )
    if proc.returncode != 0:
        return []
    paths = [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and _history_path_allowed(line.strip(), stage_filters)
    ]
    paths = sorted(paths, reverse=True)[: max(0, limit)]
    cases: list[ProbeCase] = []
    for path in paths:
        cat = _run_docker(container, "cat", path, timeout=30)
        if cat.returncode != 0:
            continue
        try:
            payload = json.loads(cat.stdout)
        except json.JSONDecodeError:
            continue
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        stage = _stage_from_path(path)
        max_tokens = int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 900)
        temperature = float(payload.get("temperature", 0.7))
        cases.append(
            ProbeCase(
                name=f"history:{stage}:{Path(path).name[:24]}",
                messages=[dict(message) for message in messages if isinstance(message, dict)],
                temperature=temperature,
                max_tokens=max_tokens,
                source_path=path,
                stage_key=stage,
            )
        )
    return cases


def build_minimax_payload(
    *,
    profile: dict[str, str],
    case: ProbeCase,
) -> dict[str, Any]:
    effective_messages = LLMClient._effective_messages_for_profile(profile, case.messages)
    effective_tokens = LLMClient._effective_max_tokens_for_profile(profile, case.max_tokens)
    effective_temperature = LLMClient._effective_temperature_for_profile(profile, case.temperature)
    payload: dict[str, Any] = {
        "model": profile["model"],
        "messages": effective_messages,
        LLMClient._max_tokens_payload_key_for_profile(profile): effective_tokens,
        "temperature": effective_temperature,
    }
    return payload


def attempt_case(
    *,
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    profile: dict[str, str],
    case: ProbeCase,
) -> ProbeAttempt:
    payload = build_minimax_payload(profile=profile, case=case)
    started_at = utc_now()
    started = time.perf_counter()
    prompt_chars = len(json.dumps(payload.get("messages", []), ensure_ascii=False))
    try:
        response = client.post(url, headers=headers, json=payload)
        duration_ms = int((time.perf_counter() - started) * 1000)
        response_text = response.text
        success = 200 <= response.status_code < 300
        error_category = ""
        error_message = ""
        output_chars = 0
        if success:
            try:
                body = response.json()
                output_chars = len(body["choices"][0]["message"].get("content") or "")
            except Exception as exc:  # noqa: BLE001
                success = False
                error_category = "parse_error"
                error_message = str(exc)
        else:
            error_category, error_message = parse_minimax_error(response_text)
        return ProbeAttempt(
            case_name=case.name,
            model=profile["model"],
            success=success,
            status_code=response.status_code,
            duration_ms=duration_ms,
            output_chars=output_chars,
            error_category=error_category,
            error_message=error_message[:500],
            response_preview=response_text[:500].replace("\n", " "),
            prompt_chars=prompt_chars,
            source_path=case.source_path,
            started_at=started_at,
            completed_at=utc_now(),
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeAttempt(
            case_name=case.name,
            model=profile["model"],
            success=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_category="exception",
            error_class=exc.__class__.__name__,
            error_message=str(exc)[:500],
            prompt_chars=prompt_chars,
            source_path=case.source_path,
            started_at=started_at,
            completed_at=utc_now(),
        )


def parse_minimax_error(response_text: str) -> tuple[str, str]:
    try:
        body = json.loads(response_text)
    except json.JSONDecodeError:
        return "http_error", response_text[:500]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("type") or "provider_error"), str(error.get("message") or "")
    return "provider_error", response_text[:500]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a long MiniMax prompt compatibility probe.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--duration-hours", type=float, default=12.0)
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    parser.add_argument("--output-dir", default="data/minimax_longrun")
    parser.add_argument("--docker-container", default="forwin")
    parser.add_argument("--artifact-root", default="/app/data/artifacts/projects")
    parser.add_argument("--history-limit", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20260521)
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_values = load_dotenv_values(Path(args.env_file))
    api_key = os.environ.get("MINIMAX_API_KEY") or env_values.get("MINIMAX_API_KEY", "")
    if not api_key:
        raise SystemExit("MINIMAX_API_KEY is not set in environment or .env")
    base_url = (args.base_url or env_values.get("MINIMAX_BASE_URL") or "https://api.minimaxi.com/v1").rstrip("/")
    model = args.model or env_values.get("MINIMAX_MODEL") or "MiniMax-M2.7"
    profile = {
        "id": "minimax-longrun",
        "name": "MiniMax longrun",
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }
    cases = builtin_non_code_cases()
    cases.extend(
        load_forwin_history_cases(
            container=args.docker_container,
            root=args.artifact_root,
            limit=args.history_limit,
            stage_filters=DEFAULT_STAGES,
        )
    )
    if not cases:
        raise SystemExit("No probe cases loaded")

    random.seed(args.seed)
    random.shuffle(cases)
    duration_seconds = args.duration_seconds or (args.duration_hours * 3600.0)
    deadline = time.monotonic() + duration_seconds
    max_attempts = int(args.max_attempts or 0)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / f"{run_id}.jsonl"
    summary_path = output_dir / f"{run_id}-summary.json"

    print(
        json.dumps(
            {
                "run_id": run_id,
                "cases": len(cases),
                "duration_seconds": duration_seconds,
                "timeout_seconds": args.timeout_seconds,
                "jsonl_path": str(jsonl_path),
                "summary_path": str(summary_path),
                "model": model,
                "base_url": base_url,
            },
            ensure_ascii=False,
        )
    )

    attempts: list[ProbeAttempt] = []
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(float(args.timeout_seconds), connect=min(30.0, float(args.timeout_seconds)))
    case_cycle = itertools.cycle(cases)
    try:
        with httpx.Client(timeout=timeout) as client, jsonl_path.open("a", encoding="utf-8") as jsonl:
            while time.monotonic() < deadline:
                if max_attempts and len(attempts) >= max_attempts:
                    break
                case = next(case_cycle)
                attempt = attempt_case(
                    client=client,
                    url=url,
                    headers=headers,
                    profile=profile,
                    case=case,
                )
                attempts.append(attempt)
                jsonl.write(json.dumps(asdict(attempt), ensure_ascii=False, sort_keys=True) + "\n")
                jsonl.flush()
                summary_path.write_text(
                    json.dumps(summarize_attempts(attempts), ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                if args.interval_seconds > 0:
                    time.sleep(float(args.interval_seconds))
    finally:
        summary_path.write_text(
            json.dumps(summarize_attempts(attempts), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(json.dumps(summarize_attempts(attempts), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
