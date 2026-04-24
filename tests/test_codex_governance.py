from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from forwin.codex_governance import CodexGovernedActionProcessor, CodexGovernedActionRequest
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project
from forwin.models.world_model import WorldEditProposalRow
from forwin.state.updater import StateUpdater


class CodexGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.engine = get_engine(str(Path(self.tmpdir.name) / "codex_governance.db"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_world_edit_proposal_action_creates_pending_proposal_only(self) -> None:
        with self.session_factory() as session:
            project = Project(title="Codex Governance", premise="测试 Codex 受控写入。")
            session.add(project)
            session.flush()

            result = CodexGovernedActionProcessor(session).apply(
                project_id=project.id,
                request=CodexGovernedActionRequest(
                    action_type="world_edit_proposal_create",
                    target_page_key="world:index",
                    reason="Codex suggested manual note.",
                    payload={"markdown_patch": {"note": "补充观察"}},
                ),
            )
            session.commit()

            proposals = session.query(WorldEditProposalRow).filter_by(project_id=project.id).all()
            self.assertTrue(result.ok)
            self.assertEqual(result.created_object_type, "world_edit_proposal")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0].status, "pending")
            self.assertEqual(proposals[0].source, "codex")

    def test_non_whitelisted_action_is_rejected(self) -> None:
        with self.session_factory() as session:
            project = Project(title="Codex Governance", premise="测试 Codex 受控写入。")
            session.add(project)
            session.flush()

            with self.assertRaises(ValueError):
                CodexGovernedActionProcessor(session).apply(
                    project_id=project.id,
                    request=CodexGovernedActionRequest(
                        action_type="canon_event_create",
                        target_page_key="world:index",
                        payload={"summary": "不允许直接写 canon"},
                    ),
                )

            self.assertEqual(session.query(WorldEditProposalRow).count(), 0)

    def test_prompt_trace_records_codex_metadata_columns(self) -> None:
        with self.session_factory() as session:
            project = Project(title="Codex Trace", premise="测试 Codex trace。")
            session.add(project)
            session.flush()

            trace = StateUpdater(session).save_prompt_trace(
                project_id=project.id,
                trace_scope="genesis",
                stage_key="world",
                backend="codex_bridge",
                codex_job_id="job-123",
                permission_profile="governed_write_mcp",
                fallback_used=True,
            )
            session.commit()

            self.assertEqual(trace.backend, "codex_bridge")
            self.assertEqual(trace.codex_job_id, "job-123")
            self.assertEqual(trace.permission_profile, "governed_write_mcp")
            self.assertTrue(trace.fallback_used)


if __name__ == "__main__":
    unittest.main()
