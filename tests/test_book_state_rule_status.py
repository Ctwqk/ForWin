from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.book_state import (
    BookStateCompiler,
    BookStateRepository,
    BookStateReviewGate,
)
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.writer_contract import WriterContractDeltaBuilder
from forwin.context.assembler_core.canon_quality_context import (
    _book_state_rule_invariant_constraints,
)
from forwin.models.base import Base
from forwin.models.book_state import GraphDeltaPatchRow, WorldNodeStateRow
from forwin.models.project import Project
from forwin.protocol.book_state import ApprovedGraphDeltaSet, WorldNode
from forwin.protocol.state_change import StateChangeCandidate
from forwin.protocol.writer import WriterOutput


@pytest.fixture
def rule_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        project = Project(title="Rule lifecycle", premise="Evidence", genre="mystery")
        session.add(project)
        session.flush()
        yield session, project.id
    engine.dispose()


def _create_rule(session, project_id, *, state, status="active", is_active=True):
    repo = BookStateRepository(session)
    repo.create_world_node(
        WorldNode(
            id="rule-weight-method",
            project_id=project_id,
            node_type="rule",
            name="统一箱重倒推法",
            status=status,
            is_active=is_active,
            created_at_chapter=3,
            profile={"public_version": "按统一箱重倒推新增箱数。"},
            source_refs=["chapter:3"],
        )
    )
    repo.append_world_node_state(
        project_id=project_id,
        node_id="rule-weight-method",
        node_type="rule",
        as_of_chapter=3,
        state=state,
    )
    session.commit()


def _constraints(session, project_id, *, before_chapter=4):
    return _book_state_rule_invariant_constraints(
        session=session, project_id=project_id, before_chapter=before_chapter
    )


@pytest.mark.parametrize(
    "raw_status,expected_active",
    [
        ("active", True),
        ("  ACTIVE\t", True),
        ("effective", True),
        ("enabled", True),
        ("in_force", True),
        ("生效", True),
        ("已生效", True),
        (" 有效 ", True),
        ("已启用", True),
        ("inactive", False),
        (" InActive ", False),
        ("retired", False),
        ("deleted", False),
        ("revoked", False),
        ("WITHDRAWN", False),
        (" suspended ", False),
        ("expired", False),
        ("paused", False),
        ("disabled", False),
        ("已撤回", False),
        (" 已撤销\n", False),
        ("已废止", False),
        ("已暂停", False),
        ("已停用", False),
        ("已失效", False),
        ("已过期", False),
        ("unknown", False),
        ("known", False),
        ("observing", False),
        ("待确认", False),
        ("未撤回", False),
        ("可能已生效", False),
        ("not revoked", False),
        ("", False),
        ("  ", False),
        (None, False),
    ],
)
def test_only_explicitly_active_rule_status_imposes_invariant(
    rule_session, raw_status, expected_active
):
    session, project_id = rule_session
    _create_rule(session, project_id, state={"status": raw_status})

    constraints = _constraints(session, project_id)

    assert bool(constraints) is expected_active
    if expected_active:
        assert constraints[0]["status"] == "active"
        assert constraints[0]["current_value"] == {
            "public_version": "按统一箱重倒推新增箱数。"
        }
        assert constraints[0]["constraints"] == {
            "immutable_definition": True,
            "requires_explicit_bridge": True,
        }
        assert constraints[0]["allowed_bridges"] == ["supersede", "retcon", "revoke"]
    raw = session.scalar(select(WorldNodeStateRow))
    assert json.loads(raw.state_json) == {"status": raw_status}


@pytest.mark.parametrize(
    "node_status,expected_active",
    [
        ("active", True),
        (" ACTIVE ", True),
        ("已生效", True),
        ("已撤回", False),
        ("unknown", False),
    ],
)
def test_missing_state_status_uses_node_status(
    rule_session, node_status, expected_active
):
    session, project_id = rule_session
    _create_rule(session, project_id, state={}, status=node_status)

    assert bool(_constraints(session, project_id)) is expected_active


def test_inactive_node_does_not_impose_even_explicitly_active_state(rule_session):
    session, project_id = rule_session
    _create_rule(session, project_id, state={"status": "active"}, is_active=False)

    assert _constraints(session, project_id) == []


def test_writer_withdrawal_preserves_history_without_reimposing_rule(rule_session):
    session, project_id = rule_session
    _create_rule(session, project_id, state={"status": "active"})
    original = _constraints(session, project_id)
    assert len(original) == 1
    body = "她划掉统一箱重倒推法：这一条撤回，不同外箱和冷媒重量不能统一换算。"
    output = WriterOutput(
        project_id=project_id,
        chapter_number=4,
        title="撤回方法",
        body=body,
        end_of_chapter_summary="撤回原先的计算方法。",
        state_changes=[
            StateChangeCandidate(
                entity_name="统一箱重倒推法",
                entity_kind="rule",
                field="status",
                old_value="active",
                new_value="已撤回",
                reason=body,
            )
        ],
    )
    built = WriterContractDeltaBuilder(session).build(
        project_id=project_id,
        chapter_number=4,
        writer_output=output,
        review_verdict_id="withdrawal-review",
    )
    assert built.issues == []
    patch = built.graph_deltas[0].node_patches[0]
    assert patch.field_path == "state.status"
    assert patch.new_value == "已撤回"
    reviewed = BookStateReviewGate(session).review(
        ApprovedGraphDeltaSet(
            project_id=project_id,
            chapter_number=4,
            graph_deltas=built.graph_deltas,
        )
    )
    assert reviewed.accepted is True
    assert reviewed.approved_changes is not None
    compiled = BookStateCompiler(session).compile(reviewed.approved_changes)
    assert compiled.committed is True
    session.commit()

    projection = BookStateProjection(session)
    before = projection.load_runtime_as_of(project_id, as_of_chapter=3)
    after = projection.load_runtime_as_of(project_id, as_of_chapter=4)
    assert before.world.get_state("rule-weight-method")["status"] == "active"
    assert after.world.get_state("rule-weight-method")["status"] == "已撤回"
    assert _constraints(session, project_id, before_chapter=4) == original
    assert _constraints(session, project_id, before_chapter=5) == []
    stored = session.scalar(select(GraphDeltaPatchRow))
    assert json.loads(stored.new_value_json) == "已撤回"
    states = list(
        session.scalars(
            select(WorldNodeStateRow).order_by(WorldNodeStateRow.as_of_chapter)
        )
    )
    assert [json.loads(row.state_json)["status"] for row in states] == [
        "active",
        "已撤回",
    ]
