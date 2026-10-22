"""Refs in an extraction that its own input tables cannot resolve.

The translator resolves every ref against BookState and fails translation on one it cannot
resolve. The extractor runs this check first, against the tables the model was given, so the
model corrects its own ref instead of the writer rewriting a sound chapter. The checks follow
the translator's rules; the translator stays the backstop.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from forwin.protocol.book_state import ObserverType

from .chapter_delta_schema import NEW_REF_PREFIX, ChapterDeltaExtraction
from .chapter_input import ChapterExtractionInput


# Entity kinds that can hold knowledge, as the translator maps them to observers.
_OBSERVER_KINDS = frozenset({"character", "faction", "organization", "group"})


@dataclass(frozen=True)
class UnknownRef:
    path: str
    ref: str
    reason: str
    # The table id whose name or alias the ref spells, when exactly one matches.
    suggestion: str = ""


def unknown_refs(
    extraction: ChapterDeltaExtraction, extraction_input: ChapterExtractionInput,
) -> list[UnknownRef]:
    """Every ref the translator would fail to resolve, in the order it resolves them."""
    return _RefCheck(extraction, extraction_input).run()


class _RefCheck:
    def __init__(self, extraction: ChapterDeltaExtraction, extraction_input: ChapterExtractionInput) -> None:
        self.extraction = extraction
        # Intents are world nodes too; the translator resolves them wherever an entity goes.
        self.kinds = {
            **{row.id: "intent" for row in extraction_input.intents},
            **{row.id: row.kind for row in extraction_input.entities},
        }
        self.entity_labels = _labels((row.id, (row.name, *row.aliases)) for row in extraction_input.entities)
        self.locations = {row.id for row in extraction_input.locations}
        self.location_labels = _labels((row.id, (row.name,)) for row in extraction_input.locations)
        self.regions = {row.region_id for row in extraction_input.locations if row.region_id}
        self.facts = {row.fact_ref for row in extraction_input.secrets}
        self.intents = {row.id for row in extraction_input.intents}
        # Like the translator, a new site exists once its own entry resolved.
        self.new_sites: set[str] = set()
        self.unknown: list[UnknownRef] = []

    def run(self) -> list[UnknownRef]:
        extraction = self.extraction
        for index, site in enumerate(extraction.new_sites):
            path = f"new_sites[{index}]"
            if site.near_ref.strip():
                anchored = self.location(site.near_ref, f"{path}.near_ref")
            elif site.region_ref.strip():
                anchored = site.region_ref.strip() in self.regions
                if not anchored:
                    self.report(f"{path}.region_ref", site.region_ref, "is not a region_id in the location table")
            else:
                anchored = False
            if anchored and site.name.strip():
                self.new_sites.add(site.name.strip())
        movers = {movement.entity_ref.strip() for movement in extraction.movements}
        for index, change in enumerate(extraction.entity_changes):
            path = f"entity_changes[{index}]"
            ref = change.ref.strip() or (
                f"{NEW_REF_PREFIX}{change.new_entity.name.strip()}" if change.new_entity is not None else ""
            )
            kind = self.entity(ref, f"{path}.ref")
            if change.new_entity is not None and not change.ref.strip():
                kind = change.new_entity.kind
            if kind is None or change.flashback:
                continue
            field = change.field.strip()
            if field == "location_id" and kind == "character" and ref not in movers:
                self.location(str(change.new_value or ""), f"{path}.new_value")
            elif field == "holder_id" and kind == "item":
                self.holder(str(change.new_value or ""), f"{path}.new_value")
        for index, relation in enumerate(extraction.relation_changes):
            path = f"relation_changes[{index}]"
            self.relation_endpoint(relation.source_ref, f"{path}.source_ref")
            self.relation_endpoint(relation.target_ref, f"{path}.target_ref")
        for index, item in enumerate(extraction.knowledge_events):
            path = f"knowledge_events[{index}]"
            self.observer(item.observer_ref, f"{path}.observer_ref")
            if item.fact_ref.strip():
                self.fact(item.fact_ref, f"{path}.fact_ref")
            elif item.new_fact is None:
                self.report(f"{path}.fact_ref", "", "is empty and the event has no new_fact")
            if item.source_ref.strip():
                self.entity(item.source_ref, f"{path}.source_ref")
            if item.touches_secret_ref.strip():
                self.fact(item.touches_secret_ref, f"{path}.touches_secret_ref")
            if item.contradicts_ref.strip():
                self.fact(item.contradicts_ref, f"{path}.contradicts_ref")
        for index, movement in enumerate(extraction.movements):
            path = f"movements[{index}]"
            self.entity(movement.entity_ref, f"{path}.entity_ref")
            self.location(movement.to_ref, f"{path}.to_ref")
            if movement.from_ref.strip():
                self.location(movement.from_ref, f"{path}.from_ref")
        for index, transfer in enumerate(extraction.item_transfers):
            path = f"item_transfers[{index}]"
            ref = transfer.item_ref.strip() or (
                f"{NEW_REF_PREFIX}{transfer.new_item.name.strip()}" if transfer.new_item is not None else ""
            )
            self.entity(ref, f"{path}.item_ref")
            self.holder(transfer.to_ref, f"{path}.to_ref")
            if transfer.from_ref.strip():
                self.holder(transfer.from_ref, f"{path}.from_ref")
        for index, event in enumerate(extraction.events):
            path = f"events[{index}]"
            for ref_index, ref in enumerate(event.participant_refs):
                self.entity(ref, f"{path}.participant_refs[{ref_index}]")
            if event.location_ref.strip():
                self.location(event.location_ref, f"{path}.location_ref")
        for index, beat in enumerate(extraction.thread_beats):
            if beat.thread_ref.strip():
                self.report(
                    f"thread_beats[{index}].thread_ref", beat.thread_ref,
                    "names a thread the input does not list; leave thread_ref empty and use new_thread_title",
                )
        for index, interaction in enumerate(extraction.intent_interactions):
            if interaction.intent_ref.strip() not in self.intents:
                self.report(f"intent_interactions[{index}].intent_ref", interaction.intent_ref, "is not an id in the intent table")
        return self.unknown

    def report(self, path: str, ref: str, reason: str, suggestion: str = "") -> None:
        self.unknown.append(UnknownRef(path=path, ref=ref.strip(), reason=reason, suggestion=suggestion))

    def entity(self, ref: str, path: str) -> str | None:
        """The kind of an entity ref ("" for a new one the admission plan decides), None when unknown."""
        text = ref.strip()
        if text.startswith(NEW_REF_PREFIX) and text.removeprefix(NEW_REF_PREFIX).strip():
            return ""
        if text in self.kinds:
            return self.kinds[text]
        reason = "is not an id in the entity table" if text else "is empty and no new entity is declared"
        self.report(path, text, reason, self.entity_labels.get(text, ""))
        return None

    def location(self, ref: str, path: str) -> bool:
        if self._is_location(ref):
            return True
        text = ref.strip()
        reason = (
            "is not declared in new_sites" if text.startswith(NEW_REF_PREFIX)
            else "is not an id in the location table"
        )
        self.report(path, text, reason, self.location_labels.get(text.removeprefix(NEW_REF_PREFIX).strip(), ""))
        return False

    def holder(self, ref: str, path: str) -> None:
        if self._is_location(ref):
            return
        text = ref.strip()
        if text.startswith(NEW_REF_PREFIX) or text in self.kinds:
            self.entity(text, path)
            return
        suggestion = self.entity_labels.get(text) or self.location_labels.get(text, "")
        self.report(path, text, "is not an id in the entity or location table", suggestion)

    def relation_endpoint(self, ref: str, path: str) -> None:
        # A location id stands for that site's state node.
        if ref.strip() not in self.locations:
            self.entity(ref, path)

    def observer(self, ref: str, path: str) -> None:
        if ref.strip() == ObserverType.READER.value:
            return
        kind = self.entity(ref, path)
        if kind and kind not in _OBSERVER_KINDS:
            self.report(path, ref, f"is of kind {kind!r}, which cannot know facts")

    def fact(self, ref: str, path: str) -> None:
        if ref.strip() not in self.facts:
            self.report(path, ref, "is not a fact_ref in the secret list")

    def _is_location(self, ref: str) -> bool:
        text = ref.strip()
        if text.startswith(NEW_REF_PREFIX):
            return text.removeprefix(NEW_REF_PREFIX).strip() in self.new_sites
        return text in self.locations


def _labels(rows: Iterable[tuple[str, tuple[str, ...]]]) -> dict[str, str]:
    """Id by each name or alias that names exactly one row."""
    ids_by_label: dict[str, set[str]] = {}
    for row_id, labels in rows:
        for label in labels:
            if label.strip():
                ids_by_label.setdefault(label.strip(), set()).add(row_id)
    return {label: next(iter(ids)) for label, ids in ids_by_label.items() if len(ids) == 1}


__all__ = ["UnknownRef", "unknown_refs"]
