from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from forwin.llm_eval.profiles import load_eval_profiles, redact_profile
from forwin.llm_eval.reporting import summarize_attempts
from forwin.llm_eval.schemas import EvalAttemptResult
from forwin.llm_eval.validators import validate_output
from forwin.llm_eval.variants import apply_cache_buster, variant_seed
from forwin.runtime_settings import RuntimeSettingsStore


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
            profiles = load_eval_profiles(manifest_path=str(manifest), selected_ids=["kimi"])
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


def test_runtime_settings_profiles_are_loaded_without_changing_default_file() -> None:
    with TemporaryDirectory() as tmp:
        settings_path = Path(tmp) / "runtime_settings.json"
        store = RuntimeSettingsStore(str(settings_path), default_api_key="")
        store.save_profile(
            profile_id="minimax",
            name="MiniMax",
            api_key="secret-minimax",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            set_as_default=True,
        )
        before = settings_path.read_text(encoding="utf-8")

        profiles = load_eval_profiles(
            runtime_settings_path=str(settings_path),
            selected_ids=["minimax"],
        )
        after = settings_path.read_text(encoding="utf-8")

    assert [profile.id for profile in profiles] == ["minimax"]
    assert profiles[0].api_key == "secret-minimax"
    assert before == after


def test_selected_minimax_and_kimi_aliases_fallback_to_default_profiles() -> None:
    with TemporaryDirectory() as tmp:
        settings_path = Path(tmp) / "missing_runtime_settings.json"
        profiles = load_eval_profiles(
            runtime_settings_path=str(settings_path),
            selected_ids=["minimax", "kimi"],
        )

    assert [profile.id for profile in profiles] == ["minimax", "kimi"]
    assert profiles[0].provider == "minimax"
    assert "minimax" in profiles[0].base_url.lower()
    assert profiles[1].provider == "moonshot"
    assert "moonshot" in profiles[1].base_url.lower()


def test_output_validator_handles_markdown_json_missing_keys_and_prose() -> None:
    valid = validate_output(
        "```json\n{\"scenes\":[{\"scene_no\":1}]}\n```",
        expected_output_kind="json",
        schema_name="scene_breakdown",
    )
    assert valid.parse_ok is True
    assert valid.schema_ok is True
    assert valid.required_keys_missing == []

    missing = validate_output(
        "{\"not_scenes\":[]}",
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
            stage_key="state_event_extraction",
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
