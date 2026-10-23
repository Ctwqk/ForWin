from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from forwin.llm_eval.profiles import (
    load_eval_profiles,
    profile_requires_api_key,
    redact_profile,
)
from forwin.llm_eval.reporting import summarize_attempts
from forwin.llm_eval.schemas import EvalAttemptResult
from forwin.llm_eval.validators import validate_output
from forwin.llm_eval.variants import apply_cache_buster, variant_seed


def test_manifest_profiles_resolve_api_key_from_env_and_redact_secret() -> None:
    with TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "profiles.json"
        manifest.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "id": "kimi",
                            "name": "Kimi",
                            "provider": "moonshot",
                            "base_url": "https://api.moonshot.cn/v1",
                            "model": "kimi-k2.5",
                            "api_key_env": "KIMI_TEST_KEY",
                            "timeout_seconds": 12,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        old_value = os.environ.get("KIMI_TEST_KEY")
        os.environ["KIMI_TEST_KEY"] = "secret-kimi-key"
        try:
            profiles = load_eval_profiles(
                manifest_path=str(manifest), selected_ids=["kimi"]
            )
        finally:
            if old_value is None:
                os.environ.pop("KIMI_TEST_KEY", None)
            else:
                os.environ["KIMI_TEST_KEY"] = old_value

    assert len(profiles) == 1
    assert profiles[0].api_key == "secret-kimi-key"
    assert profiles[0].timeout_seconds == 12
    redacted = redact_profile(profiles[0])
    assert redacted["api_key"] == "***"
    assert "secret-kimi-key" not in json.dumps(redacted, ensure_ascii=False)


def test_selected_provider_aliases_fallback_to_default_profiles() -> None:
    profiles = load_eval_profiles(
        selected_ids=["minimax", "kimi", "deepseek"],
    )

    assert [profile.id for profile in profiles] == ["minimax", "kimi", "deepseek"]
    assert profiles[0].provider == "minimax"
    assert "minimax" in profiles[0].base_url.lower()
    assert profiles[1].provider == "moonshot"
    assert "moonshot" in profiles[1].base_url.lower()
    assert profiles[2].provider == "deepseek"
    assert "deepseek" in profiles[2].base_url.lower()


def test_selected_codex_spark_alias_uses_cli_subscription_not_api_key() -> None:
    profiles = load_eval_profiles(
        selected_ids=["codex-spark"],
    )

    assert [profile.id for profile in profiles] == ["codex-spark"]
    assert profiles[0].provider == "codex_cli"
    assert profiles[0].base_url == "codex://cli"
    assert profiles[0].model == "gpt-5.3-codex-spark"
    assert profiles[0].api_key == ""
    assert profile_requires_api_key(profiles[0]) is False


def test_output_validator_handles_markdown_json_missing_keys_and_prose() -> None:
    valid = validate_output(
        '```json\n{"scenes":[{"scene_no":1}]}\n```',
        expected_output_kind="json",
        schema_name="scene_breakdown",
    )
    assert valid.parse_ok is True
    assert valid.schema_ok is True
    assert valid.required_keys_missing == []

    with_reasoning = validate_output(
        '<think>internal draft</think>\n{"scenes":[{"scene_no":1}]}',
        expected_output_kind="json",
        schema_name="scene_breakdown",
    )
    assert with_reasoning.parse_ok is True
    assert with_reasoning.schema_ok is True

    missing = validate_output(
        '{"not_scenes":[]}',
        expected_output_kind="json",
        schema_name="scene_breakdown",
    )
    assert missing.parse_ok is True
    assert missing.schema_ok is False
    assert missing.required_keys_missing == ["scenes"]

    prose = validate_output(
        "<<FORWIN_BODY>>\n潮声压过码头。\n<<FORWIN_SUMMARY>>\n主角抵达雾港。",
        expected_output_kind="tagged_prose",
        schema_name="writer_preview",
    )
    assert prose.parse_ok is True
    assert prose.schema_ok is True

    untagged_preview = validate_output(
        "潮声里的账本\n林夜沿着码头追查旧账，周澜在雾里压住了他的手。",
        expected_output_kind="tagged_prose",
        schema_name="writer_preview",
    )
    assert untagged_preview.parse_ok is True
    assert untagged_preview.schema_ok is True


def test_cache_buster_seed_is_stable_but_messages_are_unique_per_case() -> None:
    seed_a = variant_seed("run-1", "case-a", "minimax", 0)
    seed_b = variant_seed("run-1", "case-b", "minimax", 0)
    assert seed_a == variant_seed("run-1", "case-a", "minimax", 0)
    assert seed_a != seed_b

    messages = [{"role": "user", "content": "请只输出 JSON。"}]
    busted = apply_cache_buster(messages, run_id="run-1", variant_seed=seed_a)
    assert messages[0]["content"] == "请只输出 JSON。"
    assert "测试批次代号" in busted[0]["content"]
    assert seed_a[:12] in busted[0]["content"]


def test_summary_aggregates_transport_format_duplicate_and_grade() -> None:
    attempts = [
        EvalAttemptResult(
            run_id="run-1",
            profile_id="minimax",
            case_id="case-1",
            stage_key="scene_breakdown",
            task_family="writer",
            attempt_group_id="g1",
            http_status=200,
            duration_ms=100,
            input_chars=1000,
            output_chars=200,
            parse_ok=True,
            schema_ok=True,
            output_hash="same",
        ),
        EvalAttemptResult(
            run_id="run-1",
            profile_id="minimax",
            case_id="case-2",
            stage_key="chapter_delta_extraction",
            task_family="writer",
            attempt_group_id="g2",
            http_status=529,
            error_category="provider_overload",
            duration_ms=200,
            input_chars=1000,
            output_chars=0,
            parse_ok=False,
            schema_ok=False,
            output_hash="",
        ),
        EvalAttemptResult(
            run_id="run-1",
            profile_id="kimi",
            case_id="case-1",
            stage_key="scene_breakdown",
            task_family="writer",
            attempt_group_id="g3",
            http_status=400,
            error_category="bad_request",
            duration_ms=80,
            input_chars=900,
            output_chars=0,
            parse_ok=False,
            schema_ok=False,
            output_hash="",
        ),
    ]

    summary = summarize_attempts(attempts)

    minimax = summary["profiles"]["minimax"]
    assert minimax["total_attempts"] == 2
    assert minimax["http_529_rate"] == 0.5
    assert minimax["format_success_rate"] == 0.5
    assert minimax["grade"] == "warn"
    kimi = summary["profiles"]["kimi"]
    assert kimi["http_400_rate"] == 1.0
    assert kimi["grade"] == "fail"


def test_llm_eval_package_has_no_story_fixtures() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "forwin" / "llm_eval"
    story_terms = ("林夜", "周澜", "雾港", "罗盘", "潮雾")

    leaks = [
        f"{path.name}: {term}"
        for path in sorted(package_dir.rglob("*.py"))
        for term in story_terms
        if term in path.read_text(encoding="utf-8")
    ]

    assert leaks == []


def test_load_sample_contexts_reads_every_variant_from_file() -> None:
    from forwin.llm_eval.cases import load_sample_contexts
    from tests.llm_eval_fixtures import CONTEXTS_PATH

    contexts = load_sample_contexts(CONTEXTS_PATH)

    assert len(contexts) == 3
    assert [context.chapter_number for context in contexts] == [2, 3, 4]
    assert len({context.active_entities[0].name for context in contexts}) == 3
    assert all(context.premise and context.genre for context in contexts)


def test_load_sample_contexts_rejects_an_empty_file(tmp_path: Path) -> None:
    import pytest

    from forwin.llm_eval.cases import load_sample_contexts

    path = tmp_path / "contexts.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="no sample contexts"):
        load_sample_contexts(path)


def test_load_eval_cases_builds_prompts_from_fixture_text() -> None:
    from forwin.llm_eval.cases import load_eval_cases, load_sample_contexts
    from tests.llm_eval_fixtures import CASES_PATH, CONTEXTS_PATH

    contexts = load_sample_contexts(CONTEXTS_PATH)
    medium = load_eval_cases(CASES_PATH, contexts=contexts, suite="medium")
    smoke = load_eval_cases(CASES_PATH, contexts=contexts, suite="smoke")

    assert [case.case_id for case in medium] == [
        "writer_preview",
        "scene_breakdown",
        "genesis_brief",
        "arc_plan",
        "review_json",
        "comment_analysis",
    ]
    assert [case.case_id for case in smoke] == ["writer_preview", "scene_breakdown"]
    preview = medium[0]
    assert preview.input_snapshot == {"chapter_number": contexts[0].chapter_number}
    assert preview.messages
    assert contexts[0].premise in json.dumps(preview.messages, ensure_ascii=False)
    genesis = next(case for case in medium if case.case_id == "genesis_brief")
    assert genesis.response_format == {"type": "json_object"}
    assert genesis.messages[0]["role"] == "system"
    breakdown = next(case for case in medium if case.case_id == "scene_breakdown")
    assert contexts[0].chapter_plan_title in json.dumps(breakdown.messages, ensure_ascii=False)


def test_load_eval_cases_rejects_unknown_prompt_builder(tmp_path: Path) -> None:
    import pytest

    from forwin.llm_eval.cases import load_eval_cases, load_sample_contexts
    from tests.llm_eval_fixtures import CONTEXTS_PATH

    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "broken",
                        "stage_key": "x",
                        "task_family": "writer",
                        "prompt": {"builder": "no_such_builder"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no_such_builder"):
        load_eval_cases(path, contexts=load_sample_contexts(CONTEXTS_PATH), suite="medium")
