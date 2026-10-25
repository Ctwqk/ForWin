"""Narrative protections in force, and the protections and milestone refs a delta breaks.

The world tick checks its candidates, and the review gate a chapter's delta, against the same reading of the
``review_constraint`` payloads, so a protection means the same thing to both.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from forwin.book_state.knowledge import READER_OBSERVER
from forwin.book_state.knowledge import FACT_REF_PREFIX, is_secret_fact
from forwin.book_state.narrative import OPEN_PROTECTION_UNTIL, PROTECTION_KINDS, RELEASED_PROTECTION_STATUS
from forwin.book_state.path_patch import apply_path_patch
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import ENDED_STATUSES, WORLD_NODE_FIELDS
from forwin.protocol.book_state import GraphDelta


# The kind of a violation that ends a ref a later hard milestone or the arc's exit state needs.
MILESTONE_KIND = "milestone"
# Milestones come from the arc's beat sheet.
_MILESTONE_SOURCE = "beat_sheet"
# The knowledge states in which an observer holds a fact as known; must_not_know forbids only these.
_KNOWN_STATES = frozenset({"knows", "believes"})
_ALIVE_PATH = "state.alive_status"
_STATUS_PATH = "state.status"
_DEAD = "dead"
# The state path the must_exist and must_be_alive protections guard; must_hold_state names its own field.
_GUARDED_PATHS = {"must_exist": _STATUS_PATH, "must_be_alive": _ALIVE_PATH}


@dataclass(frozen=True)
class ActiveProtection:
    """A protection in force, read from its review_constraint node's payload."""

    node_id: str
    kind: str  # one of PROTECTION_KINDS
    ref: str
    until_chapter: int  # OPEN_PROTECTION_UNTIL leaves it open
    source: str  # who wrote it: beat_sheet, band_plan or human
    # must_not_know: who may not come to know the fact, and the fact as ``fact:<id>``.
    observer_ref: str = ""
    fact_ref: str = ""
    # must_hold_state: the field as the payload names it, and the value it keeps.
    field: str = ""
    value: Any = None


@dataclass(frozen=True)
class DeltaBoundaryViolation:
    """A protection or a milestone ref a delta breaks.

    A protection's violation copies the protection. A milestone-derived one has kind MILESTONE_KIND, an empty
    protection_node_id, the needed ref in milestone_ref and ref, and the ending change in field and value.
    """

    protection_node_id: str
    milestone_ref: str
    kind: str
    ref: str
    observer_ref: str
    fact_ref: str
    field: str
    value: Any
    until_chapter: int
    source: str
    impact_level: str
    evidence: str  # the extraction evidence of the patch or knowledge tag that breaks it


def active_protections(runtime: BookStateRuntime, *, chapter: int) -> list[ActiveProtection]:
    """The protections in force in ``chapter``, in node id order.

    A protection is in force while its node is active, its payload is not released, and its until_chapter is open
    or not before ``chapter``; a missing or non-integer until_chapter leaves it open. A payload without the fields
    its kind reads is skipped: the review gate already rejects one when it is written.
    """
    protections = []
    for node in sorted(runtime.narrative.nodes_by_id.values(), key=lambda node: node.id):
        if node.node_type != "review_constraint" or node.status != "active":
            continue
        protection = _protection(node.id, node.payload)
        if protection is not None and (
            protection.until_chapter == OPEN_PROTECTION_UNTIL or protection.until_chapter >= chapter
        ):
            protections.append(protection)
    return protections


def _protection(node_id: str, payload: Mapping[str, Any]) -> ActiveProtection | None:
    if payload.get("status") == RELEASED_PROTECTION_STATUS:
        return None
    kind, ref = payload.get("kind"), _text(payload.get("ref"))
    until = payload.get("until_chapter", OPEN_PROTECTION_UNTIL)
    common = {
        "node_id": node_id, "kind": kind, "until_chapter": until if type(until) is int else OPEN_PROTECTION_UNTIL,
        "source": _text(payload.get("source")),
    }
    if kind == "must_not_know":
        observer, fact = _text(payload.get("observer_ref")), _text(payload.get("fact_ref"))
        if not observer or not fact:
            return None
        # The ref names the observer too.
        return ActiveProtection(**common, ref=ref or observer, observer_ref=observer, fact_ref=_fact_ref(fact))
    if kind not in PROTECTION_KINDS or not ref:
        return None
    if kind == "must_hold_state":
        field = _text(payload.get("field"))
        return ActiveProtection(**common, ref=ref, field=field, value=payload.get("value")) if field else None
    return ActiveProtection(**common, ref=ref)


def world_field_path(runtime: BookStateRuntime, ref: str, field: str) -> str:
    """The field's path as the delta builder writes it: a profile field when only the profile has it, else state."""
    field = field.strip()
    if field in {"state", "profile"} or field.startswith(("state.", "profile.")):
        return field
    node = runtime.world.get_node(ref)
    fields = WORLD_NODE_FIELDS.get(str(node.node_type), {}) if node else {}
    root = field.split(".", 1)[0]
    if root in fields.get("profile", ()) and root not in fields.get("state", ()):
        return f"profile.{field}"
    return f"state.{field}"


def guarded_path(runtime: BookStateRuntime, protection: ActiveProtection) -> str:
    """The node path a state protection guards; empty for must_not_know."""
    if protection.kind == "must_hold_state":
        return world_field_path(runtime, protection.ref, protection.field)
    return _GUARDED_PATHS.get(protection.kind, "")


def paths_overlap(changed: str, guarded: str) -> bool:
    """Whether writing ``changed`` can change the value at ``guarded``: the same path, a parent or a child."""
    return changed == guarded or changed.startswith(guarded + ".") or guarded.startswith(changed + ".")


def value_at(payload: Mapping[str, Any], path: str) -> Any:
    value: Any = payload
    for key in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def breaks_protection(protection: ActiveProtection, old: Any, new: Any) -> bool:
    """Whether the guarded value going from ``old`` to ``new`` breaks a state protection."""
    if old == new:
        return False
    if protection.kind == "must_exist":
        # A destroyed or inactive ref no longer exists; one that had already ended stays so.
        return _ended(new) and not _ended(old)
    if protection.kind == "must_be_alive":
        return new == _DEAD
    return new != protection.value


def delta_boundary_violations(
    runtime: BookStateRuntime,
    delta: GraphDelta,
    *,
    chapter: int,
    milestone_refs: frozenset[str] = frozenset(),
    planned_endings: frozenset[tuple[str, str, str]] = frozenset(),
) -> list[DeltaBoundaryViolation]:
    """The protections in force in ``chapter`` that ``delta`` breaks, then the milestone refs it ends.

    A state protection compares its guarded value before the delta with the value after the delta's non-create
    node patches of its ref. A must_not_know protection is broken by a knowledge tag, flashback or not, in which its
    observer comes to know or believe its fact, as the tag's fact or, for the reader, the secret the tag touches. A
    milestone ref is broken only by ending it, a death or an ended status: milestones expect their refs to change
    otherwise. An ending in ``planned_endings``, as (ref, state path, value), is the plan's own goal and breaks
    nothing.
    """
    tags = [tag for tag in delta.metadata.get("knowledge_tags") or [] if isinstance(tag, dict)]
    violations = []
    for protection in active_protections(runtime, chapter=chapter):
        if protection.kind == "must_not_know":
            tag = next((tag for tag in tags if _learns(tag, protection)), None)
            if tag is None:
                continue
            evidence = str(tag.get("evidence") or "")
        else:
            old, new, evidence = _fold(runtime, delta, protection.ref, guarded_path(runtime, protection))
            if not breaks_protection(protection, old, new):
                continue
        violations.append(DeltaBoundaryViolation(
            protection_node_id=protection.node_id, milestone_ref="", kind=protection.kind, ref=protection.ref,
            observer_ref=protection.observer_ref, fact_ref=protection.fact_ref, field=protection.field,
            value=protection.value, until_chapter=protection.until_chapter, source=protection.source,
            impact_level=_impact(runtime, protection, milestone_refs), evidence=evidence,
        ))
    for ref in sorted(milestone_refs):
        for path in (_ALIVE_PATH, _STATUS_PATH):
            old, new, evidence = _fold(runtime, delta, ref, path)
            if _ends(path, old, new) and (ref, path, new) not in planned_endings:
                violations.append(DeltaBoundaryViolation(
                    protection_node_id="", milestone_ref=ref, kind=MILESTONE_KIND, ref=ref, observer_ref="",
                    fact_ref="", field=path, value=new, until_chapter=OPEN_PROTECTION_UNTIL,
                    source=_MILESTONE_SOURCE, impact_level="critical", evidence=evidence,
                ))
                break
    return violations


def _fold(runtime: BookStateRuntime, delta: GraphDelta, ref: str, path: str) -> tuple[Any, Any, str]:
    """The value at ``path`` before and after the delta's non-create patches of ``ref``, with the evidence of the
    last of those patches that writes it."""
    node = runtime.world.get_node(ref)
    before = {"state": deepcopy(runtime.world.get_state(ref)), "profile": deepcopy(node.profile) if node else {}}
    after = deepcopy(before)
    evidence = ""
    for patch in delta.node_patches:
        if patch.node_id != ref or str(patch.op) == "create" or not paths_overlap(patch.field_path, path):
            continue
        apply_path_patch(after, patch.field_path, deepcopy(patch.new_value), op=str(patch.op))
        evidence = patch.reason
    return value_at(before, path), value_at(after, path), evidence


def _learns(tag: Mapping[str, Any], protection: ActiveProtection) -> bool:
    if tag.get("state") not in _KNOWN_STATES:
        return False
    is_reader = tag.get("observer_type") == READER_OBSERVER
    if protection.observer_ref == READER_OBSERVER:
        observer_matches = is_reader
    else:
        observer_matches = not is_reader and tag.get("observer_id") == protection.observer_ref
    facts = {_fact_ref(_text(tag.get("fact_ref")))}
    if protection.observer_ref == READER_OBSERVER:
        # The reader is shown the secret the prose touches; an actor who touches a secret does not know it.
        facts.add(_fact_ref(_text(tag.get("touches_secret_ref"))))
    return observer_matches and protection.fact_ref in facts


def _impact(runtime: BookStateRuntime, protection: ActiveProtection, milestone_refs: Collection[str]) -> str:
    """Critical on a ref or fact a milestone needs; else by kind, a character's death, a hidden fact."""
    fact_id = protection.fact_ref.removeprefix(FACT_REF_PREFIX)
    if any(ref and ref in milestone_refs for ref in (protection.ref, protection.fact_ref, fact_id)):
        return "critical"
    if protection.kind == "must_exist":
        return "major"
    if protection.kind == "must_be_alive":
        node = runtime.world.get_node(protection.ref)
        return "major" if node is not None and node.node_type == "character" else "moderate"
    if protection.kind == "must_not_know":
        fact = runtime.world.facts_by_id.get(fact_id)
        return "major" if fact is not None and is_secret_fact(fact.sensitivity_level) else "moderate"
    return "moderate"


def _ends(path: str, old: Any, new: Any) -> bool:
    """A death at the alive path, or an ended status at the status path."""
    if path == _ALIVE_PATH:
        return new == _DEAD and old != _DEAD
    return _ended(new) and not _ended(old)


def _ended(status: Any) -> bool:
    return isinstance(status, str) and status in ENDED_STATUSES


def _fact_ref(fact: str) -> str:
    return f"{FACT_REF_PREFIX}{fact.removeprefix(FACT_REF_PREFIX)}" if fact else ""


def _text(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else ""


__all__ = [
    "ENDED_STATUSES",
    "MILESTONE_KIND",
    "ActiveProtection",
    "DeltaBoundaryViolation",
    "active_protections",
    "breaks_protection",
    "delta_boundary_violations",
    "guarded_path",
    "paths_overlap",
    "value_at",
    "world_field_path",
]
