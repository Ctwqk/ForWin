from types import SimpleNamespace

import pytest

from forwin.book_state.query import BookStateQuery
from forwin.book_state.runtime import ObjectiveWorldGraph
from forwin.canon.admission import CanonAdmissionService
from forwin.canon_names import CanonNameAnchor, find_canon_name_violations
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


@pytest.mark.parametrize(("text", "expected"), [
    ("林若曦。", []),
    ("林若曦。林若晴。", ["林若晴"]),
])
def test_canon_name_violations_skip_exact_known_names(text, expected):
    violations = find_canon_name_violations(
        [CanonNameAnchor("丙的母亲", "林若")],
        text,
        known_names={"林若曦"},
    )
    assert [violation.observed_name for violation in violations] == expected


@pytest.mark.parametrize("text", [
    "林若曦说：好。",
    "林若曦道，走吧。",
    "林若曦笑，点头。",
])
def test_canon_name_violations_skip_known_names_before_dialogue_tags(text):
    violations = find_canon_name_violations(
        [CanonNameAnchor("丙的母亲", "林若")],
        text,
        known_names={"林若曦"},
    )
    assert violations == []


@pytest.mark.parametrize("known_names", [
    {"林若曦"},
    {"林若"},
    {"林"},
])
def test_canon_name_violations_flag_unknown_names_before_dialogue_tags(known_names):
    violations = find_canon_name_violations(
        [CanonNameAnchor("丙的母亲", "林若")],
        "林若晴说：",
        known_names=known_names,
    )
    assert [violation.observed_name for violation in violations] == ["林若晴说"]


def test_canon_name_violations_still_flag_unknown_expansions():
    violations = find_canon_name_violations(
        [CanonNameAnchor("丙的母亲", "林若")], "林若曦。",
    )
    assert [violation.observed_name for violation in violations] == ["林若曦"]


@pytest.mark.parametrize(("name", "aliases", "kind", "active", "expected"), [
    ("林若曦", [], "character", True, []),
    ("乙", ["林若曦"], "character", True, []),
    ("林若曦", [], "location", True, ["林若曦"]),
    ("林若曦", [], "character", False, ["林若曦"]),
])
def test_checker_uses_cached_as_of_character_names_and_aliases_without_database(
    name, aliases, kind, active, expected,
):
    query = BookStateQuery(None)
    graph = _graph("p")
    graph.nodes_by_id["b"].name = "林若"
    other = graph.nodes_by_id["c"]
    other.name, other.aliases = name, aliases
    other.node_type, other.is_active = kind, active
    reads = []

    def load(project_id, *, as_of_chapter):
        reads.append((project_id, as_of_chapter))
        return SimpleNamespace(world=graph)

    query.projection.load_runtime_as_of = load
    checker = ContinuityChecker(SimpleNamespace(), book_state_query=query)
    issues = checker._check_canon_name_anchors(
        "p", WriterOutput(chapter_number=4, title="t", body="林若曦。", end_of_chapter_summary=""),
    )
    assert [issue.entity_names[0] for issue in issues] == expected
    assert reads == [("p", 3)]


def _kin_book_query():
    """A fixture-style book: three-character names plus a two-character one, each anchored by kinship."""
    names = {"son": "应潮生", "father": "应怀远", "pupil": "蒲念平", "master": "祁长烬", "mother": "林若"}
    nodes = [WorldNode(id=node_id, project_id="p", node_type="character", name=name) for node_id, name in names.items()]
    edges = [
        WorldEdge(id=f"e{i}", project_id="p", source_id=source, target_id=target, edge_type=kind,
                  edge_family="kinship_training", metadata={"appellation": appellation})
        for i, (source, target, kind, appellation) in enumerate((
            ("son", "father", "kin_of", "父亲"),
            ("pupil", "master", "master_of", "师尊"),
            ("master", "pupil", "master_of", "弟子"),
            ("son", "mother", "kin_of", "母亲"),
        ))
    ]
    query = BookStateQuery(None)
    query.projection.load_runtime_as_of = lambda project_id, *, as_of_chapter: SimpleNamespace(
        world=ObjectiveWorldGraph(nodes=nodes, edges=edges),
    )
    return query


def _chapter(text):
    return WriterOutput(
        chapter_number=1, title="t", body=text + "风过长街，灯火渐稀。" * 12, end_of_chapter_summary="",
    )


DIALOGUE_TAG_LINES = [
    "祁长烬道：“师尊。”",
    "应怀远笑。",
    "蒲念平问：“几时动身？”",
    "林若点头。",
    "林若皱眉，",
    "林若微笑。",
]


@pytest.mark.parametrize("line", DIALOGUE_TAG_LINES)
def test_dialogue_tag_after_anchored_name_never_fails_continuity(line):
    verdict = ContinuityChecker(SimpleNamespace(), min_chars=1, book_state_query=_kin_book_query()).check(
        "p", _chapter(line),
    )
    drift = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
    assert all(issue.severity == "warning" for issue in drift)
    assert [issue for issue in verdict.issues if issue.severity == "error"] == []
    assert verdict.verdict in {"pass", "warn"}
    assert verdict.recommended_action == "continue"


def test_genuine_drift_is_reported_as_warning_without_failing():
    verdict = ContinuityChecker(SimpleNamespace(), min_chars=1, book_state_query=_kin_book_query()).check(
        "p", _chapter("应潮生的父亲应怀远山。"),
    )
    drift = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
    assert [(issue.entity_names[:2], issue.severity) for issue in drift] == [(["应怀远山", "应怀远"], "warning")]
    assert verdict.verdict == "warn"
    assert verdict.recommended_action == "continue"


@pytest.mark.parametrize("line", DIALOGUE_TAG_LINES[:2])
def test_draft_review_does_not_fail_or_rewrite_on_anchor_drift(line):
    from forwin.naming import EntityAdmissionPlan, writer_output_admission_fingerprint
    from forwin.protocol.review import ReviewVerdict
    from forwin.review.draft_service import DraftReviewService

    output = _chapter(line)
    plan = EntityAdmissionPlan(
        project_id="p", chapter_number=1, candidate_fingerprint=writer_output_admission_fingerprint(output),
    )
    output = output.model_copy(update={"generation_meta": {"entity_admission_plan": plan.model_dump(mode="json")}})
    passing = ReviewVerdict(verdict="pass", issues=[])
    verdict = DraftReviewService(
        experience_reviewer=SimpleNamespace(review=lambda *_args, **_kwargs: passing),
        plan_reviewer=SimpleNamespace(review=lambda *_args, **_kwargs: passing),
        lint_collector=SimpleNamespace(collect=lambda _writer_output: []),
        map_movement_review_enabled=False,
        personality_review_enabled=False,
        canon_quality_review_in_hub_enabled=False,
    ).review(
        project_id="p",
        context=ChapterContextPack(
            project_id="p", project_title="t", premise="p", genre="g", setting_summary="",
            chapter_number=1, chapter_plan_title="t", chapter_plan_one_line="p", chapter_goals=[],
        ),
        writer_output=output,
        continuity_checker=ContinuityChecker(SimpleNamespace(), min_chars=1, book_state_query=_kin_book_query()),
    )
    assert [issue.rule_name for issue in verdict.issues] == ["canon_name_drift"]
    assert verdict.verdict != "fail"
    assert verdict.recommended_action != "rewrite"
    assert verdict.repair_instruction is None


@pytest.fixture
def compiled_book(request):
    engine = get_engine(postgres_test_url("canon-name-anchors"))
    init_db(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        project = Project(title="anchors", premise="p", genre="g")
        session.add(project)
        session.flush()
        graph = _graph(project.id)
        graph.nodes_by_id["b"].name, graph.nodes_by_id["c"].name = getattr(
            request, "param", ("丁", "乙"),
        )
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


@pytest.mark.parametrize("compiled_book", [("林若", "林若曦")], indirect=True, ids=["known-character"])
def test_continuity_checker_allows_known_character_with_anchored_prefix(compiled_book):
    session, project_id = compiled_book
    checker = ContinuityChecker(SimpleNamespace(session=session))
    anchors = checker.book_state.canon_name_anchors(project_id, as_of_chapter=0)
    assert [(anchor.role_label, anchor.canonical_name) for anchor in anchors] == [("丙的母亲", "林若")]
    verdict = checker.check(
        project_id, WriterOutput(chapter_number=1, title="t", body="林若曦。", end_of_chapter_summary=""),
    )
    assert not [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]


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
