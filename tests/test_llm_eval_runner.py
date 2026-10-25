from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from forwin.cli import build_parser
from forwin.llm_eval import runner as runner_module
from forwin.llm_eval.runner import EvalRunConfig, LLMReliabilityRunner
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
            stage_key="chapter_delta_extraction",
            task_family="writer",
            expected_output_kind="json",
            schema_name="chapter_delta_extraction",
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


def test_mini_real_pipeline_build_failure_closes_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class Container:
        def build_chapter_pipeline(self):
            calls.append("build")
            raise RuntimeError("pipeline construction failed")

        def close(self) -> None:
            calls.append("close")

    container = Container()
    monkeypatch.setattr(
        runner_module.RuntimeContainer,
        "from_config",
        lambda *_args, **_kwargs: container,
    )
    monkeypatch.setenv(
        "FORWIN_EVAL_DATABASE_URL",
        "postgresql+psycopg://unused/forwin",
    )
    runner = LLMReliabilityRunner(
        EvalRunConfig(
            run_id="pipeline-build-failure",
            mini_genesis_file=str(Path(__file__).parent / "fixtures/llm_eval/genesis.json"),
            artifact_root=str(tmp_path),
            mini_premise="a premise",
            mini_genre="a genre",
        )
    )
    profile = EvalProfile(
        id="failure-profile",
        base_url="https://example.invalid/v1",
        model="failure-model",
    )

    with pytest.raises(RuntimeError, match="construction failed"):
        runner.run_mini_real_for_profile(profile)

    assert calls == ["build", "close"]


def test_cli_parser_exposes_llm_eval_run_and_report_subcommands() -> None:
    parser = build_parser()
    run_args = parser.parse_args(
        [
            "llm-eval",
            "run",
            "--suite",
            "medium",
            "--profiles",
            "minimax,kimi,deepseek",
            "--artifact-root",
            "data/artifacts",
            "--context-file",
            "contexts.json",
            "--cases-file",
            "cases.json",
        ]
    )
    assert run_args.command == "llm-eval"
    assert run_args.llm_eval_command == "run"
    assert run_args.profiles == "minimax,kimi,deepseek"

    report_args = parser.parse_args(["llm-eval", "report", "--run-id", "run-1"])
    assert report_args.command == "llm-eval"
    assert report_args.llm_eval_command == "report"
    assert report_args.run_id == "run-1"


def test_llm_eval_cli_requires_context_and_cases_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as missing_context:
        parser.parse_args(["llm-eval", "run", "--cases-file", "cases.json"])
    assert "required: --context-file" in capsys.readouterr().err
    with pytest.raises(SystemExit) as missing_cases:
        parser.parse_args(["llm-eval", "run", "--context-file", "contexts.json"])
    assert "required: --cases-file" in capsys.readouterr().err

    assert missing_context.value.code == 2
    assert missing_cases.value.code == 2


def test_llm_eval_cli_parses_fixture_files_and_optional_mini_run_inputs() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "llm-eval",
            "run",
            "--context-file",
            "contexts.json",
            "--cases-file",
            "cases.json",
            "--mini-premise",
            "some premise",
            "--mini-genre",
            "some genre",
            "--mini-genesis-file",
            "genesis.json",
        ]
    )
    bare = parser.parse_args(
        [
            "llm-eval",
            "run",
            "--context-file",
            "contexts.json",
            "--cases-file",
            "cases.json",
        ]
    )

    assert args.context_file == "contexts.json"
    assert args.cases_file == "cases.json"
    assert (args.mini_premise, args.mini_genre) == ("some premise", "some genre")
    assert (bare.mini_premise, bare.mini_genre) == ("", "")
    assert args.mini_genesis_file == "genesis.json"
    assert bare.mini_genesis_file == ""


def test_llm_eval_cli_dry_run_loads_cases_from_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from forwin.llm_eval.cli import run_eval_from_args
    from tests.llm_eval_fixtures import CASES_PATH, CONTEXTS_PATH

    manifest = tmp_path / "profiles.json"
    manifest.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "dry",
                        "base_url": "https://example.invalid/v1",
                        "model": "dry-model",
                        "api_key": "unused",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "llm-eval",
            "run",
            "--dry-run",
            "--suite",
            "smoke",
            "--manifest",
            str(manifest),
            "--context-file",
            str(CONTEXTS_PATH),
            "--cases-file",
            str(CASES_PATH),
        ]
    )

    assert run_eval_from_args(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["cases"] == ["writer_preview", "scene_breakdown"]


def test_llm_eval_cli_reports_unreadable_fixture_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from forwin.llm_eval.cli import run_eval_from_args
    from tests.llm_eval_fixtures import CASES_PATH

    manifest = tmp_path / "profiles.json"
    manifest.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "dry",
                        "base_url": "https://example.invalid/v1",
                        "model": "dry-model",
                        "api_key": "unused",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "llm-eval",
            "run",
            "--dry-run",
            "--manifest",
            str(manifest),
            "--context-file",
            str(tmp_path / "missing.json"),
            "--cases-file",
            str(CASES_PATH),
        ]
    )

    assert run_eval_from_args(args) == 2
    assert "missing.json" in capsys.readouterr().out


def test_mini_real_run_is_skipped_without_premise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("mini real run must not build a pipeline without a premise")

    monkeypatch.setattr(runner_module.RuntimeContainer, "from_config", forbidden)
    profile = EvalProfile(
        id="minimax",
        provider="minimax",
        base_url="https://example.invalid/v1",
        model="m",
        api_key="secret",
    )
    case = EvalCase(
        case_id="scene",
        stage_key="scene_breakdown",
        task_family="writer",
        expected_output_kind="json",
        schema_name="scene_breakdown",
        messages=[{"role": "user", "content": "只输出 JSON"}],
    )
    # A mini run needs both metadata inputs and an explicit Genesis fixture.
    for premise, genre in (("", ""), ("", "some genre"), ("some premise", ""), ("some premise", "some genre")):
        runner = LLMReliabilityRunner(
            EvalRunConfig(
                run_id=f"skip-{bool(premise)}-{bool(genre)}",
                artifact_root=str(tmp_path),
                suite="smoke",
                live=True,
                include_mini_real_run=True,
                request_interval_seconds=0,
                mini_premise=premise,
                mini_genre=genre,
            ),
            adapter_factory=lambda selected: FakeAdapter(selected, ['{"scenes":[{"scene_no":1}]}']),
        )

        runner.run(profiles=[profile], cases=[case])

        rows = [
            json.loads(line)
            for line in runner.full_runs_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [(row["status"], row["reason"]) for row in rows] == [
            ("skipped", "skipped_mini_real")
        ]


def test_mini_real_run_seeds_genesis_before_continuing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []
    fixture = Path(__file__).parent / "fixtures/llm_eval/genesis.json"

    class Pipeline:
        def continue_project(self, project_id: str, *, max_chapters: int):
            calls.append(("continue", project_id, max_chapters))

            class Result:
                project_id = "p1"
                completed_chapters = [1, 2]
                failed_chapters: list[int] = []

            return Result()

        def close(self) -> None:
            calls.append("close")

    class Container:
        def build_chapter_pipeline(self):
            return Pipeline()

        def close(self) -> None:
            return None

    def seed(pipeline, *, pack, premise, genre, policy):
        assert isinstance(pipeline, Pipeline)
        assert pack == json.loads(fixture.read_text())
        assert policy.chapter_length.target_chars == 900
        calls.append(("seed", premise, genre))
        return "p1"

    monkeypatch.setattr(runner_module, "_seed_mini_real_project", seed)
    monkeypatch.setattr(
        runner_module.RuntimeContainer, "from_config", lambda *_a, **_k: Container()
    )
    monkeypatch.setenv("FORWIN_EVAL_DATABASE_URL", "postgresql+psycopg://unused/forwin")
    runner = LLMReliabilityRunner(
        EvalRunConfig(
            run_id="mini-real-inputs", artifact_root=str(tmp_path),
            mini_premise="a premise", mini_genre="a genre", mini_genesis_file=str(fixture),
        )
    )
    profile = EvalProfile(id="p", base_url="https://example.invalid/v1", model="m")

    payload = runner.run_mini_real_for_profile(profile)

    assert payload["status"] == "completed", payload
    assert payload["project_id"] == "p1"
    assert payload["completed_chapters"] == [1, 2]
    assert calls == [("seed", "a premise", "a genre"), ("continue", "p1", 2), "close"]


def test_mini_real_run_without_inputs_is_rejected_before_building(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("must not build a runtime container")

    monkeypatch.setattr(runner_module.RuntimeContainer, "from_config", forbidden)
    runner = LLMReliabilityRunner(
        EvalRunConfig(run_id="no-inputs", artifact_root=str(tmp_path))
    )
    profile = EvalProfile(id="p", base_url="https://example.invalid/v1", model="m")

    with pytest.raises(ValueError, match="mini_premise"):
        runner.run_mini_real_for_profile(profile)


@pytest.mark.parametrize("fixture_kind", ["missing", "non_pack", "gap", "one_chapter"])
def test_mini_real_run_rejects_invalid_genesis_before_building(monkeypatch, tmp_path, fixture_kind):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid fixtures must not construct a runtime")

    monkeypatch.setattr(runner_module.RuntimeContainer, "from_config", forbidden)
    fixture_path = ""
    if fixture_kind != "missing":
        fixture = json.loads((Path(__file__).parent / "fixtures/llm_eval/genesis.json").read_text())
        if fixture_kind == "non_pack":
            fixture = []
        elif fixture_kind == "gap":
            fixture["book_arc_blueprint"]["arcs"][0]["chapter_start"] = 2
        else:
            fixture["book_arc_blueprint"]["arcs"][0]["chapter_end"] = 1
        path = tmp_path / "genesis.json"
        path.write_text(json.dumps(fixture))
        fixture_path = str(path)
    runner = LLMReliabilityRunner(EvalRunConfig(
        run_id="bad-genesis", artifact_root=str(tmp_path), mini_premise="premise",
        mini_genre="genre", mini_genesis_file=fixture_path,
    ))
    with pytest.raises(ValueError, match="mini_genesis_file"):
        runner.run_mini_real_for_profile(EvalProfile(id="p", base_url="https://example.invalid/v1", model="m"))
