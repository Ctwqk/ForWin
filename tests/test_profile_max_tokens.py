from __future__ import annotations

import unittest

from forwin.config import InfrastructureConfig
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.writer.llm import profile_options
from forwin.writer.llm.profile_options import ProfileOptionsMixin
from forwin.writer.llm.routing import RoutingMixin


class _ProfileOptionsHarness(ProfileOptionsMixin, RoutingMixin):
    pass


class _RoutingHarness(RoutingMixin):
    profile_id = "primary"
    profile_name = "Primary"
    api_key = "primary-key"
    base_url = "https://primary.example/v1"
    model = "primary-model"
    fallback_profiles: list[dict[str, str]] = []


class ProfileMaxTokensTests(unittest.TestCase):
    def test_minimax_default_cap_stays_2048(self) -> None:
        profile = {
            "id": "env-minimax",
            "name": "MiniMax (.env)",
            "api_key": "test-key",
            "base_url": "https://api.minimaxi.com/v1",
            "model": "MiniMax-M2.7",
        }

        self.assertEqual(
            _ProfileOptionsHarness._effective_max_tokens_for_profile(profile, 16384),
            2048,
        )

    def test_minimax_invalid_or_non_positive_cap_falls_back_to_2048(self) -> None:
        for raw_cap in ("abc", "0", "-5", "  "):
            with self.subTest(raw_cap=raw_cap):
                profile = {
                    "id": "env-minimax",
                    "name": "MiniMax (.env)",
                    "api_key": "test-key",
                    "base_url": "https://api.minimaxi.com/v1",
                    "model": "MiniMax-M2.7",
                    "max_output_tokens": raw_cap,
                }

                self.assertEqual(
                    _ProfileOptionsHarness._effective_max_tokens_for_profile(
                        profile, 16384
                    ),
                    2048,
                )

    def test_minimax_non_positive_cap_logs_warning(self) -> None:
        profile = {
            "id": "env-minimax",
            "name": "MiniMax (.env)",
            "api_key": "test-key",
            "base_url": "https://api.minimaxi.com/v1",
            "model": "MiniMax-M2.7",
            "max_output_tokens": "0",
        }

        with self.assertLogs(profile_options.logger, level="WARNING") as cm:
            self.assertEqual(
                _ProfileOptionsHarness._effective_max_tokens_for_profile(profile, 16384),
                2048,
            )

        self.assertIn(
            "Invalid MiniMax max_output_tokens '0'; falling back to 2048",
            "\n".join(cm.output),
        )

    def test_minimax_profile_override_raises_cap(self) -> None:
        profile = {
            "id": "env-minimax",
            "name": "MiniMax (.env)",
            "api_key": "test-key",
            "base_url": "https://api.minimaxi.com/v1",
            "model": "MiniMax-M2.7",
            "max_output_tokens": "16384",
        }

        self.assertEqual(
            _ProfileOptionsHarness._effective_max_tokens_for_profile(profile, 16384),
            16384,
        )

    def test_request_profiles_preserves_max_output_tokens_override(self) -> None:
        harness = _RoutingHarness()
        harness.fallback_profiles = [
            {
                "id": "env-minimax",
                "name": "MiniMax (.env)",
                "api_key": "test-key",
                "base_url": "https://api.minimaxi.com/v1",
                "model": "MiniMax-M2.7",
                "max_output_tokens": "16384",
            }
        ]

        profiles = harness._request_profiles()

        self.assertEqual(profiles[1]["max_output_tokens"], "16384")

    def test_real_source_minimax_primary_profile_carries_raised_cap(self) -> None:
        config = InfrastructureConfig(
            minimax_api_key="test-key",
            minimax_base_url="https://api.minimaxi.com/v1",
            minimax_model="MiniMax-M2.7",
            minimax_max_output_tokens="16384",
        )
        policy = RuntimePolicy.for_profile("standard")
        try:
            client = RuntimeContainer._build_llm_client(config, policy)
            self.assertEqual(client.profile_id, "env-minimax")
            profiles = client._request_profiles()
        finally:
            client.close()

        self.assertEqual(profiles[0]["max_output_tokens"], "16384")
        self.assertEqual(
            _ProfileOptionsHarness._effective_max_tokens_for_profile(profiles[0], 16384),
            16384,
        )

    def test_non_minimax_unchanged(self) -> None:
        profile = {
            "id": "env-openai",
            "name": "OpenAI (.env)",
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5.3-codex-spark",
        }

        self.assertEqual(
            _ProfileOptionsHarness._effective_max_tokens_for_profile(profile, 5000),
            5000,
        )

    def test_kimi_rule_unchanged(self) -> None:
        profile = {
            "id": "env-kimi",
            "name": "Kimi (.env)",
            "api_key": "test-key",
            "base_url": "https://api.moonshot.cn/v1",
            "model": "kimi-k2.5",
        }

        self.assertEqual(
            _ProfileOptionsHarness._effective_max_tokens_for_profile(profile, 1800),
            1800,
        )
        self.assertEqual(
            _ProfileOptionsHarness._effective_max_tokens_for_profile(profile, 100),
            1800,
        )


if __name__ == "__main__":
    unittest.main()
