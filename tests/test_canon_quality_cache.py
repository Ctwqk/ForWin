from __future__ import annotations

import json

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import ArcPlanVersion, ChapterPlan, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import (
    CharacterStateTransitionRow,
    QualityAnalysisRunRow,
)
from forwin.protocol.writer import WriterOutput
from tests.postgres import postgres_test_url


class CountingFormClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.calls = 0
        self.payload = payload

    def complete_json(self, **_kwargs):
        self.calls += 1
        return self.payload


def test_quality_cache_reuses_exact_input_and_invalidates_content_or_plan_changes() -> None:
    engine = get_engine(postgres_test_url("canon-quality-analysis-cache"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(
                title="质量缓存",
                premise="主角：林青。",
                genre="悬疑",
                target_total_chapters=3,
            )
            session.add(project)
            session.flush()
            session.add(
                CharacterStateTransitionRow(
                    project_id=project.id,
                    character_name="林青",
                    chapter_number=0,
                    transition_type="life_state",
                    from_state="unknown",
                    to_state="alive",
                    payload_json='{"source":"test_fixture"}',
                )
            )
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_synopsis="第一卷",
                chapter_start=1,
                chapter_end=3,
            )
            session.add(arc)
            session.flush()
            plan = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
                one_line="林青遭遇致命事件。",
            )
            session.add(plan)
            session.flush()
            quote = "林青倒下，再无呼吸。"
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body=quote,
                end_of_chapter_summary="林青死亡。",
            )
            client = CountingFormClient(_payload(project.id, quote))

            draft_review = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                persist=False,
                mode="primary",
                llm_client=client,
                return_raw_analyzer_results=True,
            )
            canon_gate = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                draft_id="draft-1",
                persist=True,
                mode="primary",
                llm_client=client,
                return_raw_analyzer_results=True,
            )
            plan.one_line = "林青遭遇致命事件并留下线索。"
            session.flush()
            analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                persist=False,
                mode="primary",
                llm_client=client,
            )
            analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output.model_copy(update={"body": f"{quote}余波仍在扩散。"}),
                persist=False,
                mode="primary",
                llm_client=client,
            )
            session.commit()

            transitions = (
                session.query(CharacterStateTransitionRow)
                .filter_by(project_id=project.id, chapter_number=1)
                .all()
            )
            runs = session.query(QualityAnalysisRunRow).all()
            assert client.calls == 3
            assert draft_review.summary == canon_gate.summary
            assert canon_gate.draft_id == "draft-1"
            assert canon_gate.form == draft_review.form
            assert canon_gate.answers == draft_review.answers
            assert canon_gate.form.characters[0].name == "林青"
            assert "characters[0].life_state" in canon_gate.validation_report.validated
            assert (
                canon_gate.raw_analyzer_results[0]["character_transitions"][0]["payload"][
                    "draft_id"
                ]
                == "draft-1"
            )
            assert len(transitions) == 1
            assert json.loads(transitions[0].payload_json)["draft_id"] == "draft-1"
            assert len(runs) == 3
            assert all(json.loads(row.result_json)["analysis"]["draft_id"] == "" for row in runs)
    finally:
        engine.dispose()


def _payload(project_id: str, quote: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "chapter_number": 1,
        "form_schema_version": FORM_SCHEMA_VERSION,
        "characters": [
            {
                "name": "林青",
                "appears_in_chapter": True,
                "life_state": {
                    "value": "dead",
                    "evidence_quote": quote,
                    "subject_of_quote": "林青",
                    "confidence": 0.95,
                },
                "custody_state": {"value": "unknown"},
                "participation": {"value": "mentioned_only"},
            }
        ],
        "countdowns": [],
        "obligations": [],
        "open_signals": [],
        "new_observations": {},
        "chapter_summary": "表单测试。",
    }


def test_cached_obligation_review_keeps_actual_contract_answers_and_invalidates_contract_drift():
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.narrative_obligations.repository import NarrativeObligationRepository
    from forwin.narrative_obligations.resolution_evidence import (
        build_resolution_plan,
        context_obligations,
    )
    from forwin.narrative_obligations.types import NarrativeObligation
    engine = get_engine(postgres_test_url('obligation-quality-cache'))
    init_db(engine)
    factory = get_session_factory(engine)
    try:
        with factory.begin() as session:
            project = Project(title='义务缓存', premise='林青进入档案室。', genre='悬疑', target_total_chapters=20)
            session.add(project)
            session.flush()
            obligation = NarrativeObligationRepository(session).create_obligation(NarrativeObligation(
                project_id=project.id, origin_chapter_number=0, obligation_type='custom_reader_promise',
                status='active', summary='解释钥匙来源', payoff_test='解释林青钥匙来源',
                subject_refs=['林青'], deadline_chapter=12, resolution_conditions=['出示遗书作为客观证据']))
            body = '林青展示父亲遗书，铜钥匙正是父亲在临终前托付给她的。'
            class Client:
                calls = 0
                def complete_json(self, **kwargs):
                    self.calls += 1
                    content = kwargs['messages'][1]['content']
                    payload = json.loads(content[content.index('{'):])
                    form = payload['form']
                    ask = form['obligations'][0]
                    assessment = {'value': 'fulfilled', 'evidence_quote': body, 'subject_of_quote': '林青',
                        'confidence': .95, 'explanation': '父亲遗书证实铜钥匙临终托付给林青。'}
                    return {**{key: form[key] for key in ('project_id', 'chapter_number', 'form_schema_version')},
                        'obligations': [{'id': ask['id'], 'addressed': assessment, 'payoff_evidence': assessment,
                            'subject_matches': {**assessment, 'value': 'true'},
                            'condition_results': [{'condition': c, 'assessment': assessment}
                                                  for c in [ask['payoff_test'], *ask['resolution_conditions']]]}]}
            client = Client()
            output = WriterOutput(project_id=project.id, chapter_number=11, title='遗书', body=body, end_of_chapter_summary='遗书证实了钥匙来源。')
            def analyze(draft_id, obligation_gate_mode="strict"):
                return analyze_writer_output_quality(session=session, project_id=project.id, chapter_number=11,
                    writer_output=output, draft_id=draft_id, mode='primary', llm_client=client,
                    obligation_gate_mode=obligation_gate_mode)
            first = analyze('old-draft')
            cached = analyze('new-draft')
            assert client.calls == 1
            assert cached.form == first.form and cached.answers == first.answers
            assert cached.validation_report.rejected == []
            plan = build_resolution_plan(obligations=context_obligations(session, project.id, 11),
                form=cached.form, answers=cached.answers, project_id=project.id, chapter_number=11,
                candidate_id='new-candidate', draft_id=cached.draft_id, chapter_body=body)
            assert plan.resolved_obligation_ids == [obligation.id]
            assert plan.evidence[0].draft_id == 'new-draft'
            row = session.get(NarrativeObligationRow, obligation.id)
            row.resolution_conditions_json = '["出示遗书作为客观证据", "证实遗书未被伪造"]'
            session.flush()
            changed = analyze('newer-draft')
            assert client.calls == 2
            assert changed.form.obligations[0].resolution_conditions[-1] == '证实遗书未被伪造'
            assert changed.form.obligations[0].contract_fingerprint != first.form.obligations[0].contract_fingerprint
            assert row.status == 'active'
            row.deadline_chapter = 11
            session.flush()
            standard_due = analyze('standard-due')
            pulp_due = analyze('pulp-due', 'pulp_fatal')
            assert standard_due.form.obligations[0].must_resolve_now
            assert not pulp_due.form.obligations[0].must_resolve_now
            assert client.calls == 4
            assert not analyze('cached-pulp', 'pulp_fatal').form.obligations[0].must_resolve_now
            assert client.calls == 4
    finally:
        engine.dispose()
