from __future__ import annotations

from urllib.parse import urlparse

from forwin.config import is_configured

_LLM_ROUTE_POLICY_VERSION = "v5.0-policy-chains"


class RoutingMixin:
    def _chain_profile_ids(self, route: str) -> list[str]:
        chain = self.chains.get(route, self.chains.get("default", ()))
        return list(dict.fromkeys(
            profile_id for profile_id in (self.pinned_profile_id, *chain) if profile_id
        ))

    def _chain_profiles(self, route: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        profiles = []
        skipped = []
        for profile_id in self._chain_profile_ids(route):
            profile = self.profiles.get(profile_id)
            if not is_configured(profile):
                skipped.append({"profile_id": profile_id, "reason": "profile_unconfigured"})
                continue
            profiles.append(profile.model_dump())
        return profiles, skipped

    def _route_profiles_with_metadata(
        self,
        *,
        task_family: str = "",
        stage_key: str = "",
        response_format: dict | None = None,
        output_schema: dict | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        route = self._llm_task_route(
            task_family=task_family, stage_key=stage_key,
            response_format=response_format, output_schema=output_schema,
        )
        profiles, skipped = self._chain_profiles(route)
        candidate_chain = [
            self._profile_public_info(self.profiles[profile_id].model_dump())
            if profile_id in self.profiles else {"profile_id": profile_id}
            for profile_id in self._chain_profile_ids(route)
        ]
        return {
            "profiles": profiles,
            "candidate_chain": candidate_chain,
            "skipped_profiles": skipped,
        }

    @classmethod
    def _llm_task_route(
        cls,
        *,
        task_family: str = "",
        stage_key: str = "",
        response_format: dict | None = None,
        output_schema: dict | None = None,
    ) -> str:
        family = str(task_family or "").strip().lower()
        stage = str(stage_key or "").strip().lower()
        wants_json = bool(response_format or output_schema)
        if family == "world_engine" or stage in {"plan.arc_beats", "plan.band", "world.drift"}:
            return "planning_json_general"
        if "chapter_delta" in stage:
            return "canon_extraction"
        if stage in {"writer_preview", "writer_preview_fallback"}:
            return "writer_preview"
        if stage == "comment_analysis" or family == "phase4":
            return "feedback_analysis"
        if stage in {
            "chapter_review",
            "chapter_review_form",
            "chapter_review_json_repair",
            "entity_registrar",
            "repair_verification",
        } or family in {
            "chapter_review_form",
            "entity_admission",
            "reviewer",
            "review",
        }:
            return "review_json"
        if any(token in stage for token in ("chapter_rewrite", "repair")) or family == "repair":
            return "repair_generation"
        if stage in {
            "chapter_draft",
            "scene_generation",
            "scene_stitch",
        } or (family == "writer" and not wants_json):
            return "prose_generation"
        if stage in {"scene_breakdown", "genesis_brief", "brief"}:
            return "planning_json_low_risk" if wants_json else "planning_prose"
        if stage in {
            "world",
            "map",
            "story_engine",
            "book_blueprint",
            "world_seed",
        } or family in {
            "genesis",
            "planning",
        }:
            return "planning_json_general" if wants_json else "planning_prose"
        if wants_json:
            return "planning_json_general"
        return "general"

    @staticmethod
    def _profile_kind(
        profile: dict[str, str],
    ) -> str:
        text = " ".join(
            str(profile.get(key) or "").strip().lower()
            for key in ("id", "name", "base_url", "model")
        )
        if "minimax" in text or "minimaxi" in text:
            return "minimax"
        if "kimi" in text or "moonshot" in text:
            return "kimi"
        if "deepseek" in text:
            return "deepseek"
        if "gemini" in text or "generativelanguage" in text:
            return "gemini"
        if "openai" in text or "gpt-" in text:
            return "openai"
        return "other"

    @classmethod
    def _profile_public_info(
        cls,
        profile: dict[str, str],
    ) -> dict[str, str]:
        base_url = str(profile.get("base_url") or "")
        return {
            "profile_id": str(profile.get("id") or ""),
            "profile_name": str(profile.get("name") or ""),
            "model": str(profile.get("model") or ""),
            "base_url_host": urlparse(base_url).netloc or base_url,
            "provider_kind": cls._profile_kind(profile),
        }


__all__ = [
    'RoutingMixin',
]
