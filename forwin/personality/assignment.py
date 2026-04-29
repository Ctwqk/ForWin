from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .library import CharacterPersonalityLibrary
from .models import (
    CandidatePersonalitySkillReport,
    PersonalityAssignmentPreview,
    PersonalityAssignmentReport,
    PersonalityAssignmentRequest,
    PersonalityAssignmentResult,
    PersonalityAssignmentValidationReport,
    PersonalityLoadout,
    PersonalitySkillRef,
    RejectedPersonalitySkillReport,
    SelectedPersonalitySkillReport,
)


class PersonalityLoadoutAssigner:
    def __init__(
        self,
        library: CharacterPersonalityLibrary | None = None,
        *,
        report_lookup: Callable[[str], PersonalityAssignmentReport | dict[str, Any] | None] | None = None,
    ) -> None:
        self.library = library or CharacterPersonalityLibrary()
        self._report_lookup = report_lookup

    def assign(self, request: PersonalityAssignmentRequest) -> PersonalityAssignmentResult:
        if request.explicit_loadout:
            return self._explicit_result(request, request.explicit_loadout)
        if request.existing_loadout and self._is_manual_override(request.existing_assignment):
            return self._preserved_result(request, request.existing_loadout, manual_override=True)
        if request.existing_loadout:
            return self._preserved_result(request, request.existing_loadout, manual_override=False)

        catalog = self._assignment_rules()
        compatibility = self._compatibility_rules()
        scored = self._score_candidates(request, catalog)
        candidates = [item[0] for item in scored]
        selected: list[SelectedPersonalitySkillReport] = []
        rejected: list[RejectedPersonalitySkillReport] = []
        warnings: list[str] = []

        dominant_rule = self._first_candidate(scored, skill_type="trait", slot="dominant")
        confidence = self._confidence(dominant_rule[0]["score"] if dominant_rule else 0.0)
        if dominant_rule is None or confidence < 0.40:
            return self._fallback_result(request, candidates=candidates, rejected=rejected, confidence=confidence)

        dominant_candidate, dominant_config = dominant_rule
        dominant_weight = self._slot_weight(dominant_config, "dominant", default=0.70)
        loadout = PersonalityLoadout(
            dominant=PersonalitySkillRef(skill=dominant_candidate["skill"], weight=dominant_weight),
            secondary=[],
            social_mask=[],
            stress_modes=[],
            relationship_patterns=[],
            overrides={},
        )
        selected.append(
            SelectedPersonalitySkillReport(
                skill=dominant_candidate["skill"],
                slot="dominant",
                weight=dominant_weight,
                score=dominant_candidate["score"],
                reason_tags=list(dominant_candidate["reason_tags"]),
            )
        )

        for candidate, config in scored:
            if candidate["skill"] == dominant_candidate["skill"]:
                continue
            if len(loadout.secondary) >= 2:
                break
            skill_type = str(config.get("skill_type") or candidate.get("skill_type") or "")
            if candidate["score"] <= 0 or skill_type != "trait":
                continue
            if "secondary" not in _as_list(config.get("eligible_slots")):
                continue
            compat = self._compatibility_for(dominant_candidate["skill"], candidate["skill"], compatibility)
            if compat and not bool(compat.get("allowed", True)):
                rejected.append(
                    RejectedPersonalitySkillReport(
                        skill=candidate["skill"],
                        score=candidate["score"],
                        reason="compatibility_conflict",
                    )
                )
                continue
            if compat and str(compat.get("relation") or ""):
                relation = str(compat.get("relation") or "")
                note = str(compat.get("note") or "")
                warnings.append(f"compatibility_{relation}:{dominant_candidate['skill']}:{candidate['skill']}:{note}")
            ref = PersonalitySkillRef(
                skill=candidate["skill"],
                weight=self._slot_weight(config, "secondary", default=0.50),
            )
            loadout.secondary.append(ref)
            selected.append(
                SelectedPersonalitySkillReport(
                    skill=ref.skill,
                    slot="secondary",
                    weight=ref.weight,
                    score=candidate["score"],
                    reason_tags=list(candidate["reason_tags"]),
                )
            )

        for candidate, config in scored:
            if candidate["skill"] == dominant_candidate["skill"]:
                continue
            skill_type = str(config.get("skill_type") or candidate.get("skill_type") or "")
            if candidate["score"] <= 0:
                continue
            if skill_type == "social_mask" and "social_mask" in _as_list(config.get("eligible_slots")):
                ref = PersonalitySkillRef(
                    skill=candidate["skill"],
                    weight=self._slot_weight(config, "social_mask", default=0.55),
                    active_when=_as_list(config.get("default_active_when")),
                )
                if ref.active_when:
                    loadout.social_mask.append(ref)
                    selected.append(
                        SelectedPersonalitySkillReport(
                            skill=ref.skill,
                            slot="social_mask",
                            weight=ref.weight,
                            score=candidate["score"],
                            reason_tags=list(candidate["reason_tags"]),
                        )
                    )
                if len(loadout.social_mask) >= 2:
                    break

        for candidate, config in scored:
            skill_type = str(config.get("skill_type") or candidate.get("skill_type") or "")
            if skill_type != "stress_mode" or candidate["score"] <= 0:
                continue
            triggers = _as_list(config.get("default_trigger"))
            if not triggers:
                rejected.append(
                    RejectedPersonalitySkillReport(
                        skill=candidate["skill"],
                        score=candidate["score"],
                        reason="stress_mode_missing_default_trigger",
                    )
                )
                continue
            ref = PersonalitySkillRef(
                skill=candidate["skill"],
                weight=self._slot_weight(config, "stress_modes", default=0.50),
                trigger=triggers,
            )
            loadout.stress_modes.append(ref)
            selected.append(
                SelectedPersonalitySkillReport(
                    skill=ref.skill,
                    slot="stress_modes",
                    weight=ref.weight,
                    score=candidate["score"],
                    reason_tags=list(candidate["reason_tags"]),
                )
            )
            if len(loadout.stress_modes) >= 2:
                break

        validation = self.validate(loadout.model_dump(mode="json", exclude_none=True))
        if not validation.ok:
            return self._fallback_result(
                request,
                candidates=candidates,
                rejected=[
                    *rejected,
                    *[
                        RejectedPersonalitySkillReport(skill="loadout", reason=error)
                        for error in validation.errors
                    ],
                ],
                confidence=confidence,
            )

        report = PersonalityAssignmentReport(
            assignment_id=self._assignment_id(request, "auto_rule"),
            policy_version=request.policy.policy_version,
            assignment_mode="auto_rule",
            source=request.source,
            source_ref=request.source_ref,
            assigned_at=_utc_now(),
            confidence=confidence,
            status="valid" if confidence >= 0.60 else "valid_needs_review",
            selected_skills=selected,
            candidate_skills=candidates,
            rejected_skills=rejected,
            signal_summary=self._signal_summary(request),
            reason_tags=_unique(tag for item in selected for tag in item.reason_tags),
            warnings=[*warnings, *validation.warnings],
        )
        return PersonalityAssignmentResult(loadout=loadout, report=report, validation=validation)

    def preview(self, request: PersonalityAssignmentRequest) -> PersonalityAssignmentPreview:
        result = self.assign(request)
        return PersonalityAssignmentPreview.model_validate(result.model_dump(mode="json"))

    def validate(self, loadout: dict[str, Any]) -> PersonalityAssignmentValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        parsed = PersonalityLoadout.model_validate(loadout or {})
        skill_ids = parsed.active_skill_ids()
        missing = self.library.validate_skill_ids(skill_ids)
        errors.extend(f"unknown_skill:{skill_id}" for skill_id in missing)
        known = {skill.name: skill for skill in self.library.list_skills()}

        if parsed.dominant is None:
            errors.append("dominant_missing")
        elif parsed.dominant.skill in known and known[parsed.dominant.skill].skill_type != "trait":
            errors.append(f"dominant_type_mismatch:{parsed.dominant.skill}")

        if parsed.dominant is None and parsed.social_mask:
            errors.append("social_mask_as_only_mechanism")
        for ref in parsed.social_mask:
            if not ref.active_when:
                warnings.append(f"social_mask_without_active_when:{ref.skill}")
        for ref in parsed.stress_modes:
            if not ref.trigger:
                errors.append(f"stress_mode_without_trigger:{ref.skill}")
        return PersonalityAssignmentValidationReport(
            ok=not errors,
            errors=errors,
            warnings=warnings,
            unknown_skill_ids=missing,
        )

    def explain(self, report_id: str) -> PersonalityAssignmentReport:
        if self._report_lookup is None:
            raise KeyError(f"assignment report storage is not implemented: {report_id}")
        raw = self._report_lookup(report_id)
        if raw is None:
            raise KeyError(f"assignment report not found: {report_id}")
        return raw if isinstance(raw, PersonalityAssignmentReport) else PersonalityAssignmentReport.model_validate(raw)

    def _explicit_result(
        self,
        request: PersonalityAssignmentRequest,
        raw_loadout: dict[str, Any],
    ) -> PersonalityAssignmentResult:
        loadout = PersonalityLoadout.model_validate(raw_loadout)
        validation = self.validate(loadout.model_dump(mode="json", exclude_none=True))
        report = PersonalityAssignmentReport(
            assignment_id=self._assignment_id(request, "explicit_loadout"),
            policy_version=request.policy.policy_version,
            assignment_mode="explicit_loadout",
            source=request.source,
            source_ref=request.source_ref,
            assigned_at=_utc_now(),
            confidence=1.0 if validation.ok else 0.0,
            status="valid" if validation.ok else "blocked_conflict",
            manual_override=True,
            selected_skills=self._selected_from_loadout(loadout),
            signal_summary=self._signal_summary(request),
            warnings=list(validation.warnings),
        )
        return PersonalityAssignmentResult(loadout=loadout, report=report, validation=validation)

    def _preserved_result(
        self,
        request: PersonalityAssignmentRequest,
        raw_loadout: dict[str, Any],
        *,
        manual_override: bool,
    ) -> PersonalityAssignmentResult:
        loadout = PersonalityLoadout.model_validate(raw_loadout)
        validation = self.validate(loadout.model_dump(mode="json", exclude_none=True))
        report = PersonalityAssignmentReport(
            assignment_id=self._assignment_id(request, "preserve_existing"),
            policy_version=request.policy.policy_version,
            assignment_mode="preserve_existing",
            source=request.source,
            source_ref=request.source_ref,
            assigned_at=_utc_now(),
            confidence=1.0 if validation.ok else 0.0,
            status="preserved_manual" if manual_override else ("valid" if validation.ok else "blocked_conflict"),
            manual_override=manual_override,
            preserved_existing_loadout=True,
            selected_skills=self._selected_from_loadout(loadout),
            signal_summary=self._signal_summary(request),
            warnings=list(validation.warnings),
        )
        return PersonalityAssignmentResult(loadout=loadout, report=report, validation=validation)

    def _fallback_result(
        self,
        request: PersonalityAssignmentRequest,
        *,
        candidates: list[CandidatePersonalitySkillReport],
        rejected: list[RejectedPersonalitySkillReport],
        confidence: float,
    ) -> PersonalityAssignmentResult:
        fallback = self._fallback_config(request)
        dominant = fallback.get("dominant") if isinstance(fallback, dict) else {}
        loadout = PersonalityLoadout(
            dominant=PersonalitySkillRef(
                skill=str(dominant.get("skill") or "trait-quiet-observer"),
                weight=float(dominant.get("weight") or 0.45),
            ),
            secondary=[],
            social_mask=[],
            stress_modes=[],
            relationship_patterns=[],
            overrides={},
        )
        validation = self.validate(loadout.model_dump(mode="json", exclude_none=True))
        status = str(fallback.get("status") or "valid_needs_review") if isinstance(fallback, dict) else "valid_needs_review"
        if not validation.ok:
            status = "blocked_missing_skill"
        report = PersonalityAssignmentReport(
            assignment_id=self._assignment_id(request, "fallback_minimal"),
            policy_version=request.policy.policy_version,
            assignment_mode="fallback_minimal",
            source=request.source,
            source_ref=request.source_ref,
            assigned_at=_utc_now(),
            confidence=min(confidence, 0.39),
            status=status,
            selected_skills=self._selected_from_loadout(loadout),
            candidate_skills=candidates,
            rejected_skills=rejected,
            signal_summary=self._signal_summary(request),
            reason_tags=["fallback:minimal"],
            warnings=[*validation.warnings, "fallback_minimal_used"],
        )
        return PersonalityAssignmentResult(loadout=loadout, report=report, validation=validation)

    def _score_candidates(
        self,
        request: PersonalityAssignmentRequest,
        catalog: dict[str, Any],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        available = {skill.name for skill in self.library.list_skills()}
        scored: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for skill_id, config in sorted((catalog.get("skills") or {}).items()):
            if skill_id not in available:
                continue
            score = 0.0
            reason_tags: list[str] = []
            signals = config.get("signals") if isinstance(config, dict) else {}
            score += self._score_signal(
                "personality_tag",
                request.personality_tags,
                _as_list(signals.get("personality_tags")),
                40,
                reason_tags,
            )
            score += self._score_signal("role_hint", [request.role_hint, request.role_archetype], _as_list(signals.get("role_hint")), 25, reason_tags)
            score += self._score_signal("narrative_role", [request.narrative_role], _as_list(signals.get("narrative_role")), 20, reason_tags)
            score += self._score_signal("public_identity", [request.public_identity], _as_list(signals.get("public_identity")), 18, reason_tags)
            score += self._score_text("description", f"{request.description}\n{request.summary}", _as_list(signals.get("description_keywords")), 15, reason_tags)
            score += self._score_text("relationship", request.relationship_summary, _as_list(signals.get("relationship_keywords")), 12, reason_tags)
            score += self._score_text("goal", f"{request.goal}\n{request.long_term_goal}", _as_list(signals.get("goal_keywords")), 8, reason_tags)
            avoid = config.get("avoid_when") if isinstance(config, dict) else {}
            if not isinstance(avoid, dict):
                avoid = {}
            score -= self._score_text("avoid", request.description, _as_list(avoid.get("description_keywords")), 30, [])
            diversity_adjustment = self._cast_diversity_adjustment(request, skill_id)
            if diversity_adjustment:
                score += diversity_adjustment
                reason_tags.append(f"cast_diversity:{diversity_adjustment:+.0f}")
            scored.append(
                (
                    {
                        "skill": skill_id,
                        "skill_type": str(config.get("skill_type") or ""),
                        "score": max(score, 0.0),
                        "eligible_slots": _as_list(config.get("eligible_slots")),
                        "reason_tags": _unique(reason_tags),
                    },
                    config,
                )
            )
        scored.sort(key=lambda item: (-item[0]["score"], self._tie_break_rank(request, item[0]["skill"])))
        return scored

    def _score_signal(
        self,
        label: str,
        values: list[str],
        needles: list[str],
        weight: float,
        reason_tags: list[str],
    ) -> float:
        score = 0.0
        for value in values:
            for needle in needles:
                if _matches(value, needle):
                    score += weight
                    reason_tags.append(f"{label}:{needle}")
                    return score
        return score

    def _score_text(
        self,
        label: str,
        text: str,
        needles: list[str],
        weight: float,
        reason_tags: list[str],
    ) -> float:
        score = 0.0
        for needle in needles:
            if _matches(text, needle):
                score += weight
                reason_tags.append(f"{label}:{needle}")
        return score

    def _first_candidate(
        self,
        scored: list[tuple[dict[str, Any], dict[str, Any]]],
        *,
        skill_type: str,
        slot: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for candidate, config in scored:
            if candidate["score"] <= 0:
                continue
            if str(config.get("skill_type") or candidate.get("skill_type") or "") != skill_type:
                continue
            if slot not in _as_list(config.get("eligible_slots")):
                continue
            return candidate, config
        return None

    def _assignment_rules(self) -> dict[str, Any]:
        return _load_yaml(self.library.root / "catalog" / "assignment_rules.yaml")

    def _compatibility_rules(self) -> dict[str, Any]:
        return _load_yaml(self.library.root / "catalog" / "compatibility_matrix.yaml")

    def _compatibility_for(self, left: str, right: str, matrix: dict[str, Any]) -> dict[str, Any] | None:
        for rule in matrix.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            rule_left = str(rule.get("left") or "")
            rule_right = str(rule.get("right") or "")
            if {rule_left, rule_right} == {left, right}:
                return rule
        return None

    def _fallback_config(self, request: PersonalityAssignmentRequest) -> dict[str, Any]:
        payload = _load_yaml(self.library.root / "catalog" / "fallback_policy.yaml")
        fallbacks = payload.get("fallbacks") if isinstance(payload, dict) else {}
        key = "background_named_character" if request.character_class == "background_named_character" else request.policy.fallback_character_class
        fallback = fallbacks.get(key) or fallbacks.get("named_supporting_character") or {}
        return fallback if isinstance(fallback, dict) else {}

    def _slot_weight(self, config: dict[str, Any], slot: str, *, default: float) -> float:
        weights = config.get("default_weight")
        if isinstance(weights, dict) and slot in weights:
            return float(weights[slot])
        return default

    def _confidence(self, score: float) -> float:
        if score <= 0:
            return 0.0
        return round(min(0.95, 0.35 + min(score, 80.0) / 100.0 * 0.60), 2)

    def _selected_from_loadout(self, loadout: PersonalityLoadout) -> list[SelectedPersonalitySkillReport]:
        selected: list[SelectedPersonalitySkillReport] = []
        if loadout.dominant is not None:
            selected.append(
                SelectedPersonalitySkillReport(
                    skill=loadout.dominant.skill,
                    slot="dominant",
                    weight=loadout.dominant.weight,
                )
            )
        for slot, refs in (
            ("secondary", loadout.secondary),
            ("social_mask", loadout.social_mask),
            ("stress_modes", loadout.stress_modes),
            ("relationship_patterns", loadout.relationship_patterns),
        ):
            selected.extend(
                SelectedPersonalitySkillReport(skill=ref.skill, slot=slot, weight=ref.weight)
                for ref in refs
            )
        return selected

    def _signal_summary(self, request: PersonalityAssignmentRequest) -> dict[str, Any]:
        return {
            "role_hint": request.role_hint,
            "narrative_role": request.narrative_role,
            "public_identity": request.public_identity,
            "personality_tags": list(request.personality_tags),
            "importance": request.importance,
            "character_class": request.character_class,
        }

    def _assignment_id(self, request: PersonalityAssignmentRequest, mode: str) -> str:
        key = "|".join(
            [
                request.project_id,
                request.character_id,
                request.character_name,
                request.source,
                request.policy.policy_version,
                mode,
            ]
        )
        return "pa_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def _tie_break_rank(self, request: PersonalityAssignmentRequest, skill_id: str) -> int:
        key = "|".join(
            [
                request.project_id,
                request.character_id or request.character_name,
                request.policy.policy_version,
                skill_id,
            ]
        )
        return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], 16)

    def _cast_diversity_adjustment(self, request: PersonalityAssignmentRequest, skill_id: str) -> float:
        if not request.policy.cast_diversity_enabled:
            return 0.0
        count = 0
        for raw_loadout in request.existing_cast_loadouts:
            try:
                loadout = PersonalityLoadout.model_validate(raw_loadout or {})
            except Exception:
                continue
            if loadout.dominant is not None and loadout.dominant.skill == skill_id:
                count += 1
        if count <= 0:
            return 0.0
        return -float(request.policy.cast_diversity_adjustment or 0.0) * count

    def _is_manual_override(self, assignment: dict[str, Any] | None) -> bool:
        return bool(isinstance(assignment, dict) and assignment.get("manual_override"))


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _matches(text: str | list[str], needle: str) -> bool:
    if not needle:
        return False
    if isinstance(text, list):
        return any(_matches(item, needle) for item in text)
    haystack = str(text or "").strip().lower()
    target = str(needle or "").strip().lower()
    return bool(haystack and target and target in haystack)


def _unique(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
