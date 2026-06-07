from __future__ import annotations

from types import SimpleNamespace

from forwin.protocol.context import ChapterContextPack
from forwin.retrieval.broker_core.broker import RetrievalBroker
from forwin.retrieval.typed_budget import RetrievalBudget, bucket_memory_results


def test_bucket_memory_results_respects_per_type_quota() -> None:
    memories = [
        {"summary": "recent 1", "memory_type": "recent"},
        {"summary": "recent 2", "memory_type": "recent"},
        {"summary": "enemy 1", "memory_type": "enemy"},
        {"summary": "wealth 1", "memory_type": "wealth_status"},
    ]
    budget = RetrievalBudget(recent=1, enemy=1, wealth_status=1, promise=1, world=1)

    result = bucket_memory_results(memories, budget)

    assert [item["summary"] for item in result["recent"]] == ["recent 1"]
    assert [item["summary"] for item in result["enemy"]] == ["enemy 1"]
    assert [item["summary"] for item in result["wealth_status"]] == ["wealth 1"]


def test_broker_requests_raw_budget_and_returns_bucketed_memories() -> None:
    class FakeMemoryIndex:
        def __init__(self) -> None:
            self.limit = 0

        def search(self, *, project_id: str, query: str, limit: int):
            self.limit = limit
            return [
                SimpleNamespace(summary="recent 1", memory_type="recent", chapter_number=3),
                SimpleNamespace(summary="recent 2", memory_type="recent", chapter_number=2),
                SimpleNamespace(summary="enemy 1", memory_type="enemy", chapter_number=4),
                SimpleNamespace(summary="future", memory_type="enemy", chapter_number=9),
            ]

    memory_index = FakeMemoryIndex()
    budget = RetrievalBudget(recent=1, enemy=1, wealth_status=0, promise=0, relationship=0, world=0)
    broker = RetrievalBroker(max_memories=1, memory_index=memory_index, retrieval_budget=budget)
    base_pack = SimpleNamespace(
        project_id="project-1",
        chapter_number=5,
        chapter_plan_title="标题",
        chapter_plan_one_line="一句话",
        chapter_goals=[],
        active_threads=[],
        active_entities=[],
    )

    selected = broker._pick_memories(base_pack)

    assert memory_index.limit == 2
    assert [item.summary for item in selected] == ["recent 1", "enemy 1"]


def _typed_memory(summary: str, memory_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        summary=summary,
        memory_type=memory_type,
        chapter_number=1,
    )


def _pack_with_memories(memories: list[SimpleNamespace]) -> ChapterContextPack:
    return ChapterContextPack.model_construct(
        project_id="project-1",
        project_title="P",
        premise="p",
        genre="都市",
        setting_summary="s",
        chapter_number=5,
        chapter_plan_title="标题",
        chapter_plan_one_line="一句话",
        chapter_goals=[],
        previous_chapter_summaries=[],
        active_entities=[],
        active_threads=[],
        active_relations=[],
        retrieved_memories=memories,
    )


def test_trim_pack_prunes_low_priority_memories_before_obligations() -> None:
    broker = RetrievalBroker(context_budget_chars=2850)
    pack = _pack_with_memories(
        [
            _typed_memory("recent " + "x" * 260, "recent"),
            _typed_memory("promise " + "x" * 260, "promise"),
            _typed_memory("enemy " + "x" * 260, "enemy"),
        ]
    )

    trimmed = broker._trim_pack(pack)

    assert [item.memory_type for item in trimmed.retrieved_memories] == ["promise", "enemy"]


def test_finalize_context_summary_reports_memory_pruning() -> None:
    broker = RetrievalBroker(context_budget_chars=2850)
    base_pack = _pack_with_memories(
        [
            _typed_memory("recent " + "x" * 260, "recent"),
            _typed_memory("promise " + "x" * 260, "promise"),
            _typed_memory("enemy " + "x" * 260, "enemy"),
        ]
    )
    trimmed = broker._trim_pack(base_pack)

    broker._finalize_context_summary(
        base_pack=base_pack,
        pack=trimmed,
        memories=list(base_pack.retrieved_memories),
    )

    assert broker.last_observability_summary["memories_count_before"] == 3
    assert broker.last_observability_summary["memories_count_after"] == 2
    assert broker.last_observability_summary["pruned_memories"] == 1
