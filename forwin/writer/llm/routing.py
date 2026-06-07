from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.model_adapter import ModelCapabilities

logger = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_LLM_ROUTE_POLICY_VERSION = "v3.8-stage-aware-hard-replacement"
_ATTEMPT_RECORDED_ATTR = "_forwin_llm_attempt_recorded"


class RoutingMixin:
    def _request_profiles(self) -> list[dict[str, str]]:
        candidates = [
            {
                "id": str(self.profile_id or ""),
                "name": str(self.profile_name or ""),
                "api_key": self.api_key,
                "base_url": self.base_url,
                "model": self.model,
            }
        ]
        candidates.extend(self.fallback_profiles)
        profiles: list[dict[str, str]] = []
        seen: dict[tuple[str, str, str], int] = {}
        for item in candidates:
            profile = {
                "id": str(item.get("id", "")).strip(),
                "name": str(item.get("name", "")).strip(),
                "api_key": str(item.get("api_key", "")).strip(),
                "base_url": str(item.get("base_url", "")).strip().rstrip("/"),
                "model": str(item.get("model", "")).strip(),
            }
            if not profile["api_key"] or not profile["base_url"] or not profile["model"]:
                continue
            key = (profile["api_key"], profile["base_url"], profile["model"])
            if key in seen:
                existing = profiles[seen[key]]
                if not existing.get("id") and profile.get("id"):
                    existing["id"] = profile["id"]
                if not existing.get("name") and profile.get("name"):
                    existing["name"] = profile["name"]
                continue
            seen[key] = len(profiles)
            profiles.append(profile)
        return profiles

    @classmethod
    def _route_profiles(
        cls,
        profiles: list[dict[str, str]],
        *,
        task_family: str = "",
        stage_key: str = "",
        response_format: dict | None = None,
        output_schema: dict | None = None,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> list[dict[str, str]]:
        return cls._route_profiles_with_metadata(
            profiles,
            task_family=task_family,
            stage_key=stage_key,
            response_format=response_format,
            output_schema=output_schema,
            preferred_provider_kind=preferred_provider_kind,
            preferred_model=preferred_model,
        )["profiles"]

    @classmethod
    def _route_profiles_with_metadata(
        cls,
        profiles: list[dict[str, str]],
        *,
        task_family: str = "",
        stage_key: str = "",
        response_format: dict | None = None,
        output_schema: dict | None = None,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> dict[str, list[dict[str, str]]]:
        route = cls._llm_task_route(
            task_family=task_family,
            stage_key=stage_key,
            response_format=response_format,
            output_schema=output_schema,
        )
        candidate_chain = [cls._profile_public_info(profile) for profile in profiles]
        skipped_profiles: list[dict[str, str]] = []
        kinds = {cls._profile_kind(profile) for profile in profiles}
        has_kimi = "kimi" in kinds
        primary_kind = cls._profile_kind(profiles[0]) if profiles else ""
        has_deepseek_or_kimi = has_kimi or "deepseek" in kinds
        indexed = list(enumerate(profiles))
        replacement_filtered: list[tuple[int, dict[str, str]]] = []
        for index, profile in indexed:
            kind = cls._profile_kind(profile)
            reason = ""
            if (
                kind == "deepseek"
                and has_kimi
                and primary_kind != "deepseek"
                and route not in {"prose_generation", "repair_generation"}
            ):
                reason = "replaced_by_kimi"
            elif kind == "kimi" and primary_kind == "deepseek":
                reason = "primary_deepseek_no_kimi_fallback"
            elif kind == "gemini" and has_deepseek_or_kimi:
                reason = "replaced_by_deepseek" if "deepseek" in kinds else "replaced_by_kimi"
            if reason:
                skipped_profiles.append(
                    {
                        **cls._profile_public_info(profile),
                        "reason": reason,
                        "llm_task_route": route,
                    }
                )
                continue
            replacement_filtered.append((index, profile))
        suitable = [
            (index, profile)
            for index, profile in replacement_filtered
            if cls._profile_suitable_for_route(profile, route)
        ]
        for index, profile in replacement_filtered:
            if (index, profile) not in suitable:
                skipped_profiles.append(
                    {
                        **cls._profile_public_info(profile),
                        "reason": "route_not_allowed",
                        "llm_task_route": route,
                    }
                )
        routed = suitable
        preferred_kind = str(preferred_provider_kind or "").strip().lower()
        preferred_model_text = str(preferred_model or "").strip().lower()
        if preferred_kind or preferred_model_text:
            preferred = [
                item
                for item in routed
                if cls._profile_preference_rank(
                    item[1],
                    preferred_provider_kind=preferred_kind,
                    preferred_model=preferred_model_text,
                )
                < 99
            ]
            if preferred:
                routed.sort(
                    key=lambda item: (
                        cls._profile_preference_rank(
                            item[1],
                            preferred_provider_kind=preferred_kind,
                            preferred_model=preferred_model_text,
                        ),
                        cls._profile_route_priority(item[1], route),
                        item[0],
                    )
                )
                return {
                    "profiles": [profile for _index, profile in routed],
                    "candidate_chain": candidate_chain,
                    "skipped_profiles": skipped_profiles,
                }
        primary = (
            next((item for item in routed if item[0] == 0), None)
            if primary_kind == "deepseek"
            else None
        )
        if primary is not None:
            rest = [item for item in routed if item[0] != 0]
            rest.sort(
                key=lambda item: (
                    cls._profile_route_priority(item[1], route),
                    item[0],
                )
            )
            routed = [primary, *rest]
        else:
            routed.sort(
                key=lambda item: (
                    cls._profile_route_priority(item[1], route),
                    item[0],
                )
            )
        return {
            "profiles": [profile for _index, profile in routed],
            "candidate_chain": candidate_chain,
            "skipped_profiles": skipped_profiles,
        }

    @classmethod
    def _profile_preference_rank(
        cls,
        profile: dict[str, str],
        *,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> int:
        model = str(profile.get("model") or "").strip().lower()
        kind = cls._profile_kind(profile)
        if preferred_model:
            if model == preferred_model:
                return 0
            if preferred_model in model or model in preferred_model:
                return 1
        if preferred_provider_kind and kind == preferred_provider_kind:
            return 2
        return 99

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
        if any(token in stage for token in ("state_event", "thread_time", "lore_timeline")):
            return "canon_extraction"
        if stage in {"writer_preview", "writer_preview_fallback", "chapter_preview_fallback"}:
            return "writer_preview"
        if stage in {"comment_analysis", "npc_intents", "world_pressure"} or family in {
            "feedback",
            "phase4",
            "reader_feedback",
        }:
            return "feedback_analysis"
        if stage in {
            "chapter_review",
            "chapter_review_form",
            "chapter_review_json_repair",
            "repair_verification",
        } or family in {"chapter_review_form", "reviewer", "review"}:
            return "review_json"
        if any(token in stage for token in ("chapter_rewrite", "repair")) or family == "repair":
            return "repair_generation"
        if stage in {
            "chapter_draft",
            "scene_generation",
            "scene_stitch",
        } or (family == "writer" and not wants_json):
            return "prose_generation"
        if stage == "provisional_preview":
            return "prose_generation"
        if stage == "chapter_preview":
            return "writer_preview"
        if stage in {"scene_breakdown", "genesis_brief", "brief", "arc_plan"} or stage.startswith("launch_arc_"):
            return "planning_json_low_risk" if wants_json else "planning_prose"
        if stage in {
            "world",
            "map",
            "story_engine",
            "book_blueprint",
            "bootstrap",
            "band_plan",
            "chapter_plan",
        } or family in {
            "genesis",
            "planning",
            "arc_planning",
            "world_model",
        }:
            return "planning_json_general" if wants_json else "planning_prose"
        if wants_json:
            return "planning_json_general"
        return "general"

    @classmethod
    def _profile_suitable_for_route(cls, profile: dict[str, str], route: str) -> bool:
        kind = cls._profile_kind(profile)
        if kind == "gemini":
            return False
        if kind == "minimax" and route in {
            "prose_generation",
            "repair_generation",
            "canon_extraction",
            "planning_json_general",
            "general",
        }:
            return False
        return True

    @classmethod
    def _profile_route_priority(cls, profile: dict[str, str], route: str) -> int:
        kind = cls._profile_kind(profile)
        priorities = {
            "prose_generation": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "other": 3,
                "minimax": 99,
            },
            "repair_generation": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "other": 3,
                "minimax": 99,
            },
            "canon_extraction": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "other": 4,
                "minimax": 99,
            },
            "writer_preview": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "minimax": 4,
                "other": 5,
            },
            "planning_json_low_risk": {
                "spark": 0,
                "kimi": 1,
                "minimax": 2,
                "openai": 3,
                "deepseek": 4,
                "other": 5,
            },
            "planning_json_general": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "other": 4,
                "minimax": 99,
            },
            "planning_prose": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "other": 4,
                "minimax": 99,
            },
            "review_json": {
                "spark": 0,
                "kimi": 1,
                "minimax": 2,
                "openai": 3,
                "deepseek": 4,
                "other": 5,
            },
            "feedback_analysis": {
                "spark": 0,
                "kimi": 1,
                "minimax": 2,
                "openai": 3,
                "deepseek": 4,
                "other": 5,
            },
        }
        route_priorities = priorities.get(
            route,
            {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "other": 4,
                "minimax": 99,
            },
        )
        return int(route_priorities.get(kind, route_priorities.get("other", 50)))

    @staticmethod
    def _profile_kind(profile: dict[str, str]) -> str:
        text = " ".join(
            str(profile.get(key) or "").strip().lower()
            for key in ("id", "name", "base_url", "model")
        )
        if "codex-spark" in text or "gpt-5.3-codex-spark" in text:
            return "spark"
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
    def _profile_public_info(cls, profile: dict[str, str]) -> dict[str, str]:
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
