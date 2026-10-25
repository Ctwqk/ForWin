"""Database-free regressions for chapter-conflict disclosure and authorized repair passes."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace as NS

import pytest

from tests.test_chapter_extraction_conflicts import (
    CHAPTER, HERO, HERO_NAME, HUMAN, PASSING, PROJECT_ID, PROPOSITION, SECRET, SHADOW, SHADOW_NAME,
    _failing, _recorder, _violation, _writer_output,
)

from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.map_graph import MapGraph
from forwin.generation.pipeline_core import chapter_conflicts as stage_module
from forwin.protocol.book_state import FactNode, WorldNode
from forwin.review.repair import service
from forwin.world_engine import chapter_conflicts as conflicts_module



def _runtime():
    return BookStateRuntime(project_id=PROJECT_ID, as_of_chapter=CHAPTER - 1, map_graph=MapGraph(),
        world=ObjectiveWorldGraph(nodes=[
            WorldNode(id=HERO, project_id=PROJECT_ID, node_type="character", name=HERO_NAME),
            WorldNode(id=SHADOW, project_id=PROJECT_ID, node_type="character", name=SHADOW_NAME,
                      profile={"visibility_level": "hidden"}),
            WorldNode(id="rule", project_id=PROJECT_ID, node_type="rule", name="Concealed-Rule"),
        ], states_by_node_id={"rule": {"visibility_level": "secret"}}, facts=[
            FactNode(id=SECRET, project_id=PROJECT_ID, proposition=PROPOSITION,
                     related_node_refs=[HERO, SHADOW], sensitivity_level="secret"),
        ]))


def _draft(monkeypatch, evidence):
    monkeypatch.setattr(conflicts_module.BookStateProjection, "load_runtime_as_of", lambda *a, **k: _runtime())
    monkeypatch.setattr(conflicts_module.BookStateQuery, "current_story_minute", lambda *a, **k: 0)
    return _recorder(NS())._drafts(PROJECT_ID, CHAPTER, "candidate", [
        _violation(kind="must_not_know", ref="", observer_ref=HERO, fact_ref=f"fact:{SECRET}", evidence=evidence),
    ], detected_by="draft_review")[0]


def _render(change):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    source = Path("forwin/ui_assets/home/app_task_control.js").read_text()
    renderer = source[source.index("    function renderWorldConflictDetail("):source.index("    async function renderWorldConflictsCard(")]
    script = """
const fs = require('fs');
const conflict = JSON.parse(fs.readFileSync(0, 'utf8'));
const isChapterConflict = c => c.source === 'chapter_extraction';
const UNNAMED_ENTITY_LABEL = 'unnamed';
const createNode = (_, text) => text;
const lines = [];
""" + renderer + "\nrenderWorldConflictDetail({appendChild: text => lines.push(text)}, conflict); console.log(lines.join('\\n'));"
    return subprocess.run([node, "-e", script], input=json.dumps({"source": "chapter_extraction", "proposed_change": change}),
                          text=True, capture_output=True, check=True).stdout


@pytest.mark.parametrize("evidence", [
    f"{SHADOW_NAME} explains {PROPOSITION} under Concealed-Rule.",
    f"{SHADOW_NAME} explains {' '.join(PROPOSITION)} under Concealed-Rule.",
    f"{SHADOW_NAME} invokes Concealed-Rule.",
])
def test_conflict_evidence_projection_hides_truth_and_concealed_names(monkeypatch, evidence):
    draft = _draft(monkeypatch, evidence)
    assert draft.proposed_change["boundary"]["evidence"] == evidence
    safe = draft.proposed_change["safe_evidence"]
    rendered = _render(draft.proposed_change)
    for text in (safe, rendered):
        assert PROPOSITION not in "".join(text.split())
        assert SHADOW_NAME not in text
        assert "Concealed-Rule" not in text
    assert safe and safe in rendered


def test_conflict_evidence_projection_keeps_harmless_quote(monkeypatch):
    quote = "The observer reads a sealed letter."
    draft = _draft(monkeypatch, quote)
    assert draft.proposed_change["safe_evidence"] == quote
    assert quote in _render(draft.proposed_change)


def test_conflict_card_never_falls_back_to_raw_evidence(monkeypatch):
    change = _draft(monkeypatch, f"{PROPOSITION} {SHADOW_NAME}").proposed_change
    change.pop("safe_evidence", None)
    rendered = _render(change)
    assert PROPOSITION not in rendered and SHADOW_NAME not in rendered


@pytest.mark.parametrize("first_decider", ["codex", "human"])
def test_new_human_rewrite_gets_budget_over_saved_draft(monkeypatch, first_decider):
    attempts, writes, patches, histories = [], [], [], []
    saved = _writer_output("Saved draft body.")
    current = NS(draft=NS(id="saved", llm_raw_response="saved-artifact"), output=saved)
    candidate = NS(id="candidate", candidate_draft_id="saved")
    session = NS(commit=lambda: None, add=lambda row: None, get=lambda *a: current.draft)
    plan = NS(project_id=PROJECT_ID, chapter_number=CHAPTER, repair_attempt_count=0)
    repo = NS(list_chapter_rewrite_attempts=lambda *a: list(attempts))
    def save_attempt(**kw):
        row = NS(id=f"attempt-{len(attempts)}", design_patch_json=json.dumps(kw.pop("design_patch")), **kw)
        attempts.append(row)
        return row
    updater = NS(save_review=lambda *a: NS(id="review"), save_chapter_rewrite_attempt=save_attempt)
    monkeypatch.setattr(service.BookStateProjection, "load_runtime_as_of", lambda *a, **k: _runtime())
    monkeypatch.setattr(service, "_latest_draft_and_review_for_chapter", lambda **kw: (current.draft, None))
    monkeypatch.setattr(service, "_sync_candidate_repair_history", lambda *a, **kw: histories.append(kw["attempts"]))
    monkeypatch.setattr(service, "_apply_final_residual_decision", lambda *a, **kw: (kw["current_output"], kw["current_review"], False))
    def patch(*args, **kw):
        patches.append(kw)
        return {}, kw["context"], {}, {}, ""
    monkeypatch.setattr(service, "_apply_repair_patch", patch)
    success = False
    def write(**kw):
        writes.append(kw)
        return _writer_output("Repaired saved draft." if success else f"Saved draft attempt {len(writes)}.")
    def persist(**kw):
        current.draft = NS(id=f"draft-{len(writes)}", llm_raw_response="saved-artifact")
        current.output = kw["writer_output"]
        return current.output, current.draft, NS(id="repaired-review")
    execution = NS(policy=HUMAN, _pause_requested=lambda: False,
        _record_decision_event=lambda **kw: NS(id="event"), _record_rule_decision_event=lambda **kw: NS(id="rule-event"),
        _review_event_payload=lambda r: {}, _chapter_plan_snapshot=lambda **kw: {}, _band_plan_snapshot=lambda **kw: {},
        _emit_progress=lambda *a, **kw: None, _write_chapter_with_attention_fallback=write,
        _save_prompt_trace_payload=lambda **kw: "", _plan_writer_output_entities=lambda **kw: kw["writer_output"],
        _review_current_output=lambda **kw: PASSING if success else _failing(_violation()),
        _apply_canon_name_drift_autofix=lambda *a: None,
        _apply_placeholder_leakage_autofix=lambda *a: None, _review_with_repair_verification=lambda **kw: kw["review"],
        _persist_draft_and_review=persist)
    def conflict(id, decider):
        draft = conflicts_module.chapter_conflict_draft(_violation(), project_id=PROJECT_ID, chapter_number=CHAPTER, story_time=0)
        return NS(id=id, proposed_change_json=json.dumps(draft.proposed_change),
                  decision_json=json.dumps({"option": "rewrite_chapter", "decided_by": decider}))
    first = conflict("first", first_decider)
    context = NS(chapter_plan_title="Saved chapter", chapter_plan_one_line="", chapter_goals=[])
    helpers = dict(session=session, repo=repo, updater=updater, checker=None, project_id=PROJECT_ID,
                   chapter_plan=plan, context=context, writer_output=saved, conflicts=[first])
    service._repair_world_conflict(execution, **helpers)
    limit = HUMAN.review.effective_rewrite_limit(has_blocking_issue=True)
    assert len(attempts) == len(writes) == limit
    prior = list(attempts)
    saved = current.output
    saved_draft_id = current.draft.id
    helpers["writer_output"] = saved
    candidate.candidate_draft_id = saved_draft_id
    # Re-entering the same authorization must not grant another pass.
    service._repair_world_conflict(execution, **helpers)
    assert len(writes) == limit
    new = conflict("human-next", "human")
    attached = []
    conflict_repo = NS(pending_chapter_resumes=lambda *a, **kw: [new], get=lambda *a, **kw: new,
                       attach_result_refs=lambda row, refs: attached.extend(refs))
    monkeypatch.setattr(stage_module, "WorldConflictRepository", lambda session: conflict_repo)
    monkeypatch.setattr(stage_module, "CandidateDraftRepository", lambda session: NS(latest_for_chapter=lambda **kw: candidate))
    stage = NS(repair=service.RepairService(), repair_execution=execution,
               _load_writer_output_from_meta=lambda value: saved)
    success = True
    resumed = stage_module.ChapterConflicts(stage, session, repo, updater, None, HUMAN, plan, NS()).resume()
    assert resumed[0].body == "Repaired saved draft."
    assert len(writes) == limit + 1
    assert all(call["trace_stage_key"] == "chapter_rewrite" for call in writes)
    assert patches[-1]["current_output"] is saved
    assert patches[-1]["repair_instruction"].must_fix
    assert attempts[:limit] == prior and len(histories[-1]) == limit + 1
    assert attempts[-1].attempt_no == limit + 1 and attempts[-1].phase_attempt_no == 1
    assert attempts[-1].source_draft_id == saved_draft_id
    assert json.loads(attempts[-1].design_patch_json)["world_conflict_ids"] == [new.id]
    assert attached == ["candidate:candidate"]


def test_amend_resume_announces_review_before_saved_text_review(monkeypatch):
    calls = []
    saved = _writer_output("Saved draft body.")
    row = NS(id="amend", decision_json='{"option":"amend_plan"}')
    monkeypatch.setattr(stage_module, "WorldConflictRepository", lambda session: NS(
        pending_chapter_resumes=lambda *a, **kw: [row], get=lambda *a, **kw: row,
        attach_result_refs=lambda *a: None))
    monkeypatch.setattr(stage_module, "CandidateDraftRepository", lambda session: NS(
        latest_for_chapter=lambda **kw: NS(id="candidate", candidate_draft_id="saved")))
    def review(**kw):
        calls.append(("review", kw["writer_output"]))
        return saved, PASSING, False
    stage = NS(_emit_progress=lambda *a, **kw: calls.append(("progress", kw)),
               repair_execution=None, repair=NS(review_candidate=review),
               _load_writer_output_from_meta=lambda value: saved)
    session = NS(get=lambda *a: NS(llm_raw_response="saved"), commit=lambda: None)
    plan = NS(project_id=PROJECT_ID, chapter_number=CHAPTER)
    stage_module.ChapterConflicts(stage, session, None, None, None, HUMAN, plan, None).resume()
    assert calls == [("progress", {"stage": "continuity_review", "project_id": PROJECT_ID,
                                  "current_chapter": CHAPTER}), ("review", saved)]
