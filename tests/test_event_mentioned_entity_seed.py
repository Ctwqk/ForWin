from __future__ import annotations

from dataclasses import dataclass

from forwin.orchestrator_loop_core.world_projection import (
    _ensure_event_mentioned_non_character_entities,
    _filter_resolvable_events,
)
from forwin.protocol.state_change import EventCandidate
from forwin.protocol.subworld import EntityMention
from forwin.protocol.writer import WriterOutput


@dataclass
class FakeEntity:
    kind: str
    name: str


class FakeRepo:
    def __init__(self, entities: dict[str, FakeEntity] | None = None) -> None:
        self.entities = dict(entities or {})

    def get_entities_by_names(self, project_id: str, names: list[str]) -> dict[str, FakeEntity]:
        return {
            name: self.entities[name]
            for name in names
            if name in self.entities
        }


class FakeUpdater:
    def __init__(self, repo: FakeRepo) -> None:
        self.repo = repo
        self.created: list[dict[str, object]] = []

    def create_entity(
        self,
        *,
        project_id: str,
        kind: str,
        name: str,
        description: str,
        aliases: list[str],
        importance: int,
        chapter: int,
    ) -> FakeEntity:
        entity = FakeEntity(kind=kind, name=name)
        self.repo.entities[name] = entity
        self.created.append(
            {
                "project_id": project_id,
                "kind": kind,
                "name": name,
                "description": description,
                "aliases": aliases,
                "importance": importance,
                "chapter": chapter,
            }
        )
        return entity


def _writer_output(
    *,
    events: list[EventCandidate],
    mentions: list[EntityMention],
) -> WriterOutput:
    return WriterOutput(
        chapter_number=35,
        title="第35章",
        body="",
        end_of_chapter_summary="",
        new_events=events,
        entity_mentions=mentions,
    )


def test_event_mentioned_faction_is_seeded_before_event_filtering() -> None:
    event = EventCandidate(
        summary="城发集团巡查队提前抵达，林陈撤离",
        significance="major",
        involved_entity_names=["林陈", "城发集团巡查队"],
        roles=["protagonist", "antagonist"],
    )
    output = _writer_output(
        events=[event],
        mentions=[
            EntityMention(
                entity_name="城发集团巡查队",
                entity_kind="组织",
                is_named=True,
                is_on_stage=True,
            )
        ],
    )
    repo = FakeRepo({"林陈": FakeEntity(kind="character", name="林陈")})
    updater = FakeUpdater(repo)

    created = _ensure_event_mentioned_non_character_entities(
        repo,
        updater,
        "project-1",
        35,
        output,
    )
    filtered = _filter_resolvable_events(repo, "project-1", 35, output.new_events)

    assert created == 1
    assert updater.created[0]["kind"] == "faction"
    assert updater.created[0]["name"] == "城发集团巡查队"
    assert filtered == [event]


def test_event_mentioned_character_is_not_auto_seeded() -> None:
    event = EventCandidate(
        summary="未知人物交出钥匙",
        significance="minor",
        involved_entity_names=["未知人物"],
        roles=["mentioned"],
    )
    output = _writer_output(
        events=[event],
        mentions=[
            EntityMention(
                entity_name="未知人物",
                entity_kind="character",
                is_named=True,
                is_on_stage=True,
            )
        ],
    )
    repo = FakeRepo()
    updater = FakeUpdater(repo)

    created = _ensure_event_mentioned_non_character_entities(
        repo,
        updater,
        "project-1",
        35,
        output,
    )
    filtered = _filter_resolvable_events(repo, "project-1", 35, output.new_events)

    assert created == 0
    assert updater.created == []
    assert filtered == []
