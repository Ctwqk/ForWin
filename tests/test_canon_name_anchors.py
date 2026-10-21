from types import SimpleNamespace

import pytest

from forwin.book_state.query import BookStateQuery
from forwin.book_state.runtime import ObjectiveWorldGraph
from forwin.canon.admission import CanonAdmissionService
from forwin.checker.rules import ContinuityChecker
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.book_state import ApprovedGraphDeltaSet, EdgePatch, GraphDelta, NodePatch, WorldEdge, WorldNode
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.writer import WriterOutput
from tests.postgres import postgres_test_url


def _graph(project_id):
    nodes = [WorldNode(id=node_id, project_id=project_id, node_type=kind, name=name)
             for node_id, kind, name in (("a", "character", "丙"), ("b", "character", "丁"),
                                        ("c", "character", "乙"), ("place", "location", "家"),
                                        ("unnamed", "character", ""))]
    edges = [WorldEdge(id=str(i), project_id=project_id, source_id=source, target_id=target,
                       edge_type=kind, edge_family="kinship_training" if kind in {"kin_of", "master_of"} else "social", is_active=active, metadata=metadata)
             for i, (source, target, kind, active, metadata) in enumerate((
                 ("a", "b", "kin_of", True, {"appellation": "母亲"}),
                 ("a", "b", "kin_of", True, {"appellation": "母亲"}),
                 ("b", "a", "family_of", True, {"appellation": "子女"}),
                 ("a", "c", "mentor_of", True, {"appellation": "导师"}),
                 ("a", "c", "master_of", True, {"appellation": "师父"}),
                 ("a", "c", "kin_of", True, {}),
                 ("a", "c", "kin_of", True, {"appellation": "  "}),
                 ("a", "c", "kin_of", False, {"appellation": "父亲"}),
                 ("a", "c", "enemy_of", True, {"appellation": "敌人"}),
                 ("a", "place", "kin_of", True, {"appellation": "家"}),
                 ("a", "unnamed", "kin_of", True, {"appellation": "亲属"}),
                 ("place", "a", "kin_of", True, {"appellation": "住户"}),
                 ("unnamed", "a", "kin_of", True, {"appellation": "亲属"}),
             ))]
    return ObjectiveWorldGraph(nodes=nodes, edges=edges)


def test_anchor_filtering_deduplication_and_sorting_without_database():
    query = BookStateQuery(None)
    reads = []
    def load(project_id, *, as_of_chapter):
        reads.append((project_id, as_of_chapter))
        return SimpleNamespace(world=_graph(project_id))
    query.projection.load_runtime_as_of = load
    anchors = query.canon_name_anchors("p", as_of_chapter=3)
    assert [(a.role_label, a.canonical_name) for a in anchors] == sorted([
        ("丙的母亲", "丁"), ("丁的子女", "丙"), ("丙的导师", "乙"), ("丙的师父", "乙"),
    ])
    assert reads == [("p", 3)]


@pytest.fixture
def compiled_book():
    engine = get_engine(postgres_test_url("canon-name-anchors"))
    init_db(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        project = Project(title="anchors", premise="p", genre="g")
        session.add(project)
        session.flush()
        graph = _graph(project.id)
        world = ObjectiveWorldGraph(
            nodes=[graph.nodes_by_id[key] for key in ("a", "b", "c")],
            edges=[graph.edges_by_id[key] for key in ("0", "5")],
        )
        result = CanonAdmissionService().commit_world_seed(
            session=session, project_id=project.id, revision_id="anchor-seed",
            approved_changes=ApprovedGraphDeltaSet(
                project_id=project.id, chapter_number=0,
                graph_deltas=[GraphDelta(
                    id="anchor-seed", project_id=project.id, chapter_number=0, source_type="genesis_seed",
                    node_patches=[NodePatch(node_id=n.id, node_type=n.node_type, op="create", new_value=n.model_dump(mode="json")) for n in world.nodes_by_id.values()],
                    edge_patches=[EdgePatch(edge_id=e.id, op="create", source_id=e.source_id, target_id=e.target_id,
                                            edge_type=e.edge_type, edge_family=e.edge_family,
                                            new_value=e.model_dump(mode="json")) for e in world.edges_by_id.values()],
                )],
            ),
        )
        assert result.committed
        yield session, project.id
    engine.dispose()


def test_anchors_come_from_kinship_edges_with_appellation(compiled_book):
    session, project_id = compiled_book
    anchors = BookStateQuery(session).canon_name_anchors(project_id, as_of_chapter=0)
    assert [(a.role_label, a.canonical_name) for a in anchors] == [("丙的母亲", "丁")]



def test_continuity_checker_flags_drift_from_book_state_anchor(compiled_book):
    session, project_id = compiled_book
    verdict = ContinuityChecker(SimpleNamespace(session=session)).check(
        project_id, WriterOutput(chapter_number=1, title="t", body="丙的母亲名叫丁戊。", end_of_chapter_summary=""),
    )
    issues = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
    assert any(issue.entity_names[:2] == ["丁戊", "丁"] for issue in issues)


def test_writer_builders_receive_context_anchors():
    from forwin.writer.prompt_core.builders import build_single_chapter_draft_prompt, build_preview_chapter_prompt, build_scene_breakdown_prompt
    context = ChapterContextPack(
        project_title="t", premise="p", genre="g", setting_summary="s", chapter_number=1,
        chapter_plan_title="t", chapter_plan_one_line="p", chapter_goals=[],
        canon_name_anchors=[{"role_label": "丙的母亲", "canonical_name": "丁"}],
    )
    for builder in (build_single_chapter_draft_prompt, build_preview_chapter_prompt, build_scene_breakdown_prompt):
        content = "\n".join(message["content"] for message in builder(context))
        assert "丙的母亲姓名：丁" in content


def test_state_provider_carries_anchors_into_context_pack(compiled_book):
    from forwin.context.assembler_core.assembler import ChapterContextAssembler
    from forwin.context.providers.state_provider import StateContextProvider
    from forwin.state.repo import StateRepository

    session, project_id = compiled_book
    pack = ChapterContextAssembler(providers=[StateContextProvider()], gates=[]).assemble(
        StateRepository(session), project_id,
        SimpleNamespace(chapter_number=1, title="t", one_line="p"),
    )
    assert [(a.role_label, a.canonical_name) for a in pack.canon_name_anchors] == [("丙的母亲", "丁")]
