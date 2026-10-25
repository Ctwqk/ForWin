from __future__ import annotations

import json
import os
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any, Callable

import httpx

from forwin.config import InfrastructureConfig, ModelProfileConfig
from forwin.genesis import BookGenesisService
from forwin.genesis.constants import GENESIS_STAGE_ORDER
from forwin.genesis.handoff.commands import StartWritingCommand
from forwin.maintenance.post_canon import save_world_prompt_trace
from forwin.planning.beat_sheet.service import ArcBeatSheetService
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.writer.llm import OpenAICompatibleAdapter

from .reporting import render_markdown_summary, summarize_attempts
from .schemas import EvalAttemptResult, EvalCase, EvalProfile, EvalRunConfig
from .validators import validate_output
from .variants import apply_cache_buster, variant_seed


AdapterFactory = Callable[[EvalProfile], Any]


def _load_mini_genesis_pack(path: str) -> dict[str, Any]:
    """Load caller-owned story data; runtime code carries no evaluation story fixture."""
    if not path.strip():
        raise ValueError("mini real run needs mini_genesis_file")
    pack = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(pack, dict) or any(
        not isinstance(pack.get(key), dict) or not pack[key]
        for key in ("world", "world_seed", "book_arc_blueprint")
    ):
        raise ValueError("mini_genesis_file must contain world, world_seed and book_arc_blueprint objects")
    arcs = pack["book_arc_blueprint"].get("arcs")
    if not isinstance(arcs, list) or not arcs:
        raise ValueError("mini_genesis_file must contain blueprint arcs")
    next_chapter = 1
    for arc in arcs:
        if not isinstance(arc, dict) or int(arc.get("chapter_start", 0)) != next_chapter:
            raise ValueError("mini_genesis_file arc ranges must cover chapters consecutively from 1")
        end = int(arc.get("chapter_end", 0))
        if end < next_chapter:
            raise ValueError("mini_genesis_file contains an empty arc range")
        next_chapter = end + 1
    if next_chapter < 3:
        raise ValueError("mini_genesis_file must cover at least two chapters")
    return pack


def _seed_mini_real_project(
    pipeline, *, pack: dict[str, Any], premise: str, genre: str, policy: RuntimePolicy,
) -> str:
    """Lock the supplied Genesis fixture and perform the normal writing handoff."""
    genesis = BookGenesisService(
        llm_client=pipeline.llm_client,
        beat_sheets=ArcBeatSheetService(
            pipeline.llm_client,
            save_trace=partial(save_world_prompt_trace, artifact_store=getattr(pipeline, "artifact_store", None)),
        ),
    )
    with pipeline._SessionFactory.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title=premise[:60], premise=premise, genre=genre, runtime_policy=policy,
            target_total_chapters=int(pack["book_arc_blueprint"]["arcs"][-1]["chapter_end"]),
        )
        revision = genesis.create_initial_revision(session=session, updater=updater, project=project)
        revision = genesis.patch_pack(
            session=session, updater=updater, project=project, revision=revision, patch=pack,
        )
        for stage_key in GENESIS_STAGE_ORDER:
            revision = genesis.lock_stage(
                session=session, updater=updater, project=project, revision=revision, stage_key=stage_key,
            )
        genesis.handoff.start_writing(
            session=session, updater=updater, command=StartWritingCommand(project_id=project.id),
        )
        return project.id


def _json_dump_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _error_category_for_status(status_code: int) -> str:
    if status_code == 429:
        return "rate_limit"
    if status_code in {500, 502, 503, 504, 529}:
        return "provider_overload"
    if status_code in {401, 403}:
        return "auth"
    if 400 <= status_code < 500:
        return "bad_request"
    return "unknown" if status_code else "network"


def _status_from_exception(exc: BaseException) -> int:
    response = getattr(exc, "response", None)
    return int(getattr(response, "status_code", 0) or 0)


def _input_chars(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False))


class LLMReliabilityRunner:
    def __init__(
        self,
        config: EvalRunConfig,
        *,
        adapter_factory: AdapterFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.adapter_factory = adapter_factory or self._default_adapter_factory
        self.sleep = sleep
        self.run_dir = (
            Path(config.artifact_root)
            / "llm_eval"
            / "runs"
            / config.run_id
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.attempts_path = self.run_dir / "attempts.jsonl"
        self.full_runs_path = self.run_dir / "full_runs.jsonl"

    @staticmethod
    def _default_adapter_factory(profile: EvalProfile) -> Any:
        adapter = OpenAICompatibleAdapter(
            profiles={profile.id: ModelProfileConfig(
                id=profile.id, name=profile.name, api_key=profile.api_key,
                base_url=profile.base_url, model=profile.model,
                max_output_tokens=profile.max_output_tokens,
            )},
            chains={"default": (profile.id,)},
            timeout_seconds=profile.timeout_seconds,
            retry_attempts=1,
        )
        return adapter

    def run(self, *, profiles: list[EvalProfile], cases: list[EvalCase]) -> dict[str, Any]:
        attempts: list[EvalAttemptResult] = []
        for profile in profiles:
            attempts.extend(self.run_direct_cases(profile=profile, cases=cases))
            if self.config.include_mini_real_run:
                if not (
                    self.config.mini_premise.strip()
                    and self.config.mini_genre.strip()
                    and self.config.mini_genesis_file.strip()
                ):
                    self._record_skipped_mini_real_for_profile(
                        profile,
                        reason="skipped_mini_real",
                    )
                else:
                    self.run_mini_real_for_profile(profile)
        summary = summarize_attempts(attempts)
        summary.update(
            {
                "run_id": self.config.run_id,
                "suite": self.config.suite,
                "run_dir": str(self.run_dir),
            }
        )
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (self.run_dir / "summary.md").write_text(
            render_markdown_summary(summary),
            encoding="utf-8",
        )
        return summary

    def _record_skipped_mini_real_for_profile(self, profile: EvalProfile, *, reason: str) -> dict[str, Any]:
        payload = {
            "run_id": self.config.run_id,
            "profile_id": profile.id,
            "status": "skipped",
            "reason": reason,
        }
        _json_dump_line(self.full_runs_path, payload)
        return payload

    def run_direct_cases(
        self,
        *,
        profile: EvalProfile,
        cases: list[EvalCase],
    ) -> list[EvalAttemptResult]:
        adapter = self.adapter_factory(profile)
        results: list[EvalAttemptResult] = []
        try:
            sequence = 0
            for round_index in range(max(1, int(self.config.rounds or 1))):
                for case in cases:
                    if sequence and self.config.request_interval_seconds > 0:
                        self.sleep(self.config.request_interval_seconds)
                    seed = case.variant_seed or variant_seed(
                        self.config.run_id,
                        case.case_id,
                        profile.id,
                        round_index,
                    )
                    messages = apply_cache_buster(case.messages, run_id=self.config.run_id, variant_seed=seed)
                    started_at = time.perf_counter()
                    raw_output = ""
                    exc: BaseException | None = None
                    try:
                        raw_output = str(
                            adapter.chat(
                                messages,
                                temperature=case.temperature,
                                max_tokens=case.max_tokens,
                                response_format=case.response_format,
                                timeout_seconds=profile.timeout_seconds,
                                retry_on_timeout=True,
                                task_family=case.task_family,
                                stage_key=case.stage_key,
                                output_schema={"type": "object"} if case.response_format else None,
                            )
                        )
                    except BaseException as caught:  # noqa: BLE001
                        exc = caught
                    attempts = self._drain_attempts(adapter)
                    result = self._result_from_call(
                        profile=profile,
                        case=case,
                        messages=messages,
                        raw_output=raw_output,
                        started_at=started_at,
                        attempts=attempts,
                        exc=exc,
                    )
                    results.append(result)
                    _json_dump_line(self.attempts_path, result.model_dump(mode="json"))
                    sequence += 1
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        return results

    @staticmethod
    def _drain_attempts(adapter: Any) -> list[dict[str, Any]]:
        drain = getattr(adapter, "drain_llm_attempt_events", None)
        if not callable(drain):
            return []
        raw = drain()
        return [item for item in raw if isinstance(item, dict)]

    def _result_from_call(
        self,
        *,
        profile: EvalProfile,
        case: EvalCase,
        messages: list[dict],
        raw_output: str,
        started_at: float,
        attempts: list[dict[str, Any]],
        exc: BaseException | None,
    ) -> EvalAttemptResult:
        validation = validate_output(
            raw_output,
            expected_output_kind=case.expected_output_kind,
            schema_name=case.schema_name,
        )
        last_attempt = attempts[-1] if attempts else {}
        status = int(last_attempt.get("http_status") or _status_from_exception(exc) or 0)
        error_category = str(last_attempt.get("error_category") or "")
        if exc is not None and not error_category:
            error_category = "timeout" if isinstance(exc, httpx.TimeoutException) else _error_category_for_status(status)
        retry_count = max(0, len(attempts) - 1)
        clean_success = (
            exc is None
            and status < 400
            and validation.parse_ok
            and validation.schema_ok
            and retry_count == 0
        )
        return EvalAttemptResult(
            run_id=self.config.run_id,
            profile_id=profile.id,
            case_id=case.case_id,
            stage_key=case.stage_key,
            task_family=case.task_family,
            attempt_group_id=str(last_attempt.get("attempt_group_id") or uuid.uuid4().hex),
            http_status=status,
            error_category=error_category,
            timeout_kind=str(last_attempt.get("timeout_kind") or ""),
            duration_ms=int(last_attempt.get("duration_ms") or max(0, int((time.perf_counter() - started_at) * 1000))),
            retry_count=retry_count,
            input_chars=int(last_attempt.get("input_chars") or _input_chars(messages)),
            output_chars=int(validation.output_chars or last_attempt.get("output_chars") or 0),
            temperature=(
                float(last_attempt["temperature"])
                if "temperature" in last_attempt and last_attempt["temperature"] is not None
                else None
            ),
            requested_temperature=(
                float(last_attempt["requested_temperature"])
                if "requested_temperature" in last_attempt and last_attempt["requested_temperature"] is not None
                else None
            ),
            max_tokens=(
                int(last_attempt["max_tokens"])
                if "max_tokens" in last_attempt and last_attempt["max_tokens"] is not None
                else None
            ),
            requested_max_tokens=(
                int(last_attempt["requested_max_tokens"])
                if "requested_max_tokens" in last_attempt and last_attempt["requested_max_tokens"] is not None
                else None
            ),
            parse_ok=validation.parse_ok,
            schema_ok=validation.schema_ok,
            required_keys_missing=validation.required_keys_missing,
            output_hash=validation.normalized_output_hash,
            provider_request_id=str(last_attempt.get("provider_request_id") or ""),
            error_class=exc.__class__.__name__ if exc is not None else str(last_attempt.get("error_class") or ""),
            error_message=str(exc) if exc is not None else str(last_attempt.get("error_message") or ""),
            clean_success=clean_success,
        )

    def run_mini_real_for_profile(self, profile: EvalProfile) -> dict[str, Any]:
        premise = self.config.mini_premise.strip()
        genre = self.config.mini_genre.strip()
        if not premise or not genre:
            raise ValueError("mini real run needs both mini_premise and mini_genre")
        pack = _load_mini_genesis_pack(self.config.mini_genesis_file)
        database_url = os.environ.get("FORWIN_EVAL_DATABASE_URL", InfrastructureConfig.from_env().database_url)
        artifact_root = self.run_dir / f"mini_real_{profile.id}_artifacts"
        started_at = time.perf_counter()
        payload: dict[str, Any] = {
            "run_id": self.config.run_id,
            "profile_id": profile.id,
            "status": "started",
            "database_url": database_url,
            "artifact_root": str(artifact_root),
        }
        infrastructure = InfrastructureConfig(
            database_url=database_url,
            artifact_root=str(artifact_root),
            llm_env_profiles=[
                {
                    "id": "eval-primary",
                    "name": profile.name or profile.id,
                    "api_key": profile.api_key,
                    "base_url": profile.base_url,
                    "model": profile.model,
                }
            ],
            llm_timeout_seconds=profile.timeout_seconds,
            llm_retry_attempts=1,
        )
        policy = RuntimePolicy.for_profile(
            "standard",
            model_profile_id="eval-primary",
        ).with_user_settings(
            min_chars=800,
            target_chars=900,
            max_chars=1200,
            review_interval_chapters=0,
        )
        container = RuntimeContainer.from_config(
            infrastructure,
            policy=policy,
            role="generation_worker",
        )
        try:
            pipeline = container.build_chapter_pipeline()
        except Exception:
            container.close()
            raise
        try:
            project_id = _seed_mini_real_project(
                pipeline, pack=pack, premise=premise, genre=genre, policy=policy,
            )
            result = pipeline.continue_project(project_id, max_chapters=2)
            payload.update(
                {
                    "status": "completed" if not result.failed_chapters else "partial_failed",
                    "project_id": result.project_id,
                    "completed_chapters": list(result.completed_chapters),
                    "failed_chapters": list(result.failed_chapters),
                    "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                }
            )
        except Exception as exc:  # noqa: BLE001
            payload.update(
                {
                    "status": "failed",
                    "error_class": exc.__class__.__name__,
                    "error_message": str(exc),
                    "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                }
            )
        finally:
            pipeline.close()
        _json_dump_line(self.full_runs_path, payload)
        return payload
