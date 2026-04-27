from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from forwin.cli import build_parser
from forwin.codex_bridge.runner import CodexExecResult
from forwin.llm_eval.runner import CodexCliEvalAdapter, EvalRunConfig, LLMReliabilityRunner
from forwin.llm_eval.schemas import EvalCase, EvalProfile


class FakeAdapter:
    def __init__(self, profile: EvalProfile, responses: list[object]) -> None:
        self.profile_id = profile.id
        self.profile_name = profile.name
        self.model = profile.model
        self.base_url = profile.base_url
        self.responses = responses
        self.llm_attempt_events: list[dict[str, object]] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            self.llm_attempt_events.append(
                {
                    "attempt_group_id": "group-fail",
                    "attempt_no": 1,
                    "http_status": getattr(getattr(response, "response", None), "status_code", 0) or 0,
                    "error_category": "provider_overload",
                    "duration_ms": 20,
                    "input_chars": 120,
                    "output_chars": 0,
                    "final_failure": True,
                }
            )
            raise response
        text = str(response)
        self.llm_attempt_events.append(
            {
                "attempt_group_id": "group-ok",
                "attempt_no": 1,
                "http_status": 200,
                "duration_ms": 10,
                "input_chars": 120,
                "output_chars": len(text),
                "final_failure": False,
            }
        )
        return text

    def drain_llm_attempt_events(self):
        events = list(self.llm_attempt_events)
        self.llm_attempt_events.clear()
        return events

    def close(self) -> None:
        return None


class FakeCodexRunner:
    def __init__(self, content: str = '{"scenes":[{"scene_no":1}]}') -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def run(self, request, *, timeout_seconds: float | None = None):  # noqa: ANN001
        self.calls.append(
            {
                "prompt": request.prompt,
                "output_schema": request.output_schema,
                "model": request.model,
                "ignore_user_config": request.ignore_user_config,
                "ephemeral": request.ephemeral,
                "timeout_seconds": timeout_seconds,
            }
        )
        return CodexExecResult(ok=True, content=self.content, returncode=0)


def test_direct_runner_writes_attempts_and_summary_without_raw_prompt_or_secret() -> None:
    with TemporaryDirectory() as tmp:
        run_root = Path(tmp) / "artifacts"
        profile = EvalProfile(
            id="minimax",
            name="MiniMax",
            provider="minimax",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            api_key="secret-key",
        )
        cases = [
            EvalCase(
                case_id="scene",
                stage_key="scene_breakdown",
                task_family="writer",
                expected_output_kind="json",
                schema_name="scene_breakdown",
                messages=[{"role": "user", "content": "只输出 JSON"}],
            )
        ]
        runner = LLMReliabilityRunner(
            EvalRunConfig(
                run_id="run-test",
                artifact_root=str(run_root),
                suite="smoke",
                live=True,
                include_mini_real_run=False,
                request_interval_seconds=0,
                burst_every_seconds=0,
            ),
            adapter_factory=lambda selected_profile: FakeAdapter(
                selected_profile,
                ['{"scenes":[{"scene_no":1}]}'],
            ),
        )

        summary = runner.run(profiles=[profile], cases=cases)
        run_dir = run_root / "llm_eval" / "runs" / "run-test"
        attempts_path = run_dir / "attempts.jsonl"
        rows = [
            json.loads(line)
            for line in attempts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert summary["profiles"]["minimax"]["grade"] == "pass"
        assert rows[0]["schema_ok"] is True
        serialized = json.dumps(rows, ensure_ascii=False)
        assert "secret-key" not in serialized
        assert "只输出 JSON" not in serialized
        assert (run_dir / "summary.json").is_file()
        assert (run_dir / "summary.md").is_file()


def test_direct_runner_records_http_529_failure_category() -> None:
    request = httpx.Request("POST", "https://example.invalid/chat/completions")
    response = httpx.Response(529, json={"error": "overloaded"}, request=request)
    exc = httpx.HTTPStatusError("HTTP 529", request=request, response=response)
    with TemporaryDirectory() as tmp:
        profile = EvalProfile(
            id="kimi",
            name="Kimi",
            provider="moonshot",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k2.5",
            api_key="secret",
        )
        case = EvalCase(
            case_id="state",
            stage_key="state_event_extraction",
            task_family="writer",
            expected_output_kind="json",
            schema_name="state_event_extraction",
            messages=[{"role": "user", "content": "只输出 JSON"}],
        )
        runner = LLMReliabilityRunner(
            EvalRunConfig(
                run_id="run-fail",
                artifact_root=tmp,
                suite="smoke",
                live=True,
                include_mini_real_run=False,
                request_interval_seconds=0,
                burst_every_seconds=0,
            ),
            adapter_factory=lambda selected_profile: FakeAdapter(selected_profile, [exc]),
        )

        summary = runner.run(profiles=[profile], cases=[case])

    assert summary["profiles"]["kimi"]["http_529_rate"] == 1.0
    assert summary["profiles"]["kimi"]["grade"] == "fail"


def test_codex_cli_eval_adapter_uses_codex_exec_without_api_key() -> None:
    profile = EvalProfile(
        id="codex-spark",
        name="GPT-5.3-Codex-Spark",
        provider="codex_cli",
        base_url="codex://cli",
        model="gpt-5.3-codex-spark",
        api_key="",
        timeout_seconds=12,
    )
    fake_runner = FakeCodexRunner()
    adapter = CodexCliEvalAdapter(profile, runner=fake_runner)  # type: ignore[arg-type]

    output = adapter.chat(
        [{"role": "user", "content": "只输出 JSON"}],
        response_format={"type": "json_object"},
        task_family="writer",
        stage_key="scene_breakdown",
        output_schema={"type": "object"},
    )
    attempts = adapter.drain_llm_attempt_events()

    assert output == '{"scenes":[{"scene_no":1}]}'
    assert fake_runner.calls[0]["model"] == "gpt-5.3-codex-spark"
    assert fake_runner.calls[0]["output_schema"] is None
    assert fake_runner.calls[0]["ignore_user_config"] is True
    assert fake_runner.calls[0]["ephemeral"] is True
    assert attempts[0]["backend"] == "codex_cli"
    assert attempts[0]["provider_kind"] == "spark"
    assert attempts[0]["http_status"] == 200
    assert attempts[0]["llm_task_route"] == "planning_json_low_risk"


def test_cli_parser_exposes_llm_eval_run_and_report_subcommands() -> None:
    parser = build_parser()
    run_args = parser.parse_args(
        [
            "llm-eval",
            "run",
            "--suite",
            "medium",
            "--profiles",
            "minimax,kimi,codex-spark",
            "--artifact-root",
            "data/artifacts",
        ]
    )
    assert run_args.command == "llm-eval"
    assert run_args.llm_eval_command == "run"
    assert run_args.profiles == "minimax,kimi,codex-spark"

    report_args = parser.parse_args(["llm-eval", "report", "--run-id", "run-1"])
    assert report_args.command == "llm-eval"
    assert report_args.llm_eval_command == "report"
    assert report_args.run_id == "run-1"
