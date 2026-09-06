from __future__ import annotations

import argparse
import hashlib
import py_compile
from pathlib import Path


APP_ROOT = Path("/app")

EXPECTED_BASE_SHA256 = {
    "forwin/canon/admission.py": "80fd1add05cb1a6e96cac4775b1baeeb644878348a2dcd72394bff9df340e768",
    "forwin/application/projects/reviews.py": "121f436557880a9a90249227b9552e4b52efc658f8eba13189238a117dc64832",
    "forwin/book_state/repository.py": "1a187d416403568eeb54791c617595f604419cb527586a38f27d230c5dc97e04",
    "forwin/book_state/runtime.py": "0735b09e5272ae5530b6ee709ab52b027977b8ab8620c32c4bedd077296dd2cc",
    "forwin/book_state/projection.py": "5c9cc52c82358f1f1546a5a624ac43986443b9795bfd8ba33a1757595b787d48",
}
EXPECTED_COMPATIBILITY_SENTINEL_SHA256 = {
    "forwin/context/assembler_core/canon_quality_context.py": "9c8520d3f7e6b0849a252e34250d6e4362cc5c786e7be641cd6f5b88a6e73050",
    "forwin/writer/prompt_core/constraints.py": "196b659a6e04f049c6372a721a462cae7860be5f4103f80a4c7ef3b2f9259c84",
}
# Staged verbatim from source commit 4924bf50e79245bea77c2c358ff37b21613c52cc.
EXPECTED_SOURCE_SHA256 = {
    "forwin/canon/historical_rewrite.py": "fca0286aa4a80820042a1431cee008be7b2086d39cfe076f154f25ebe66ee8b1",
    "forwin/book_state/repository.py": "baaa98b2ee2e18217cb5e364af699dbd41bbdd9df160f0af5a3c5b9bae388f8c",
    "forwin/book_state/runtime.py": "e494a1eade394ea6313794ccc6f281c3e100b7b2398d9402b445e21cd3c72be0",
    "forwin/book_state/projection.py": "b84ebb60539e46525d72004b981ee4596c74023560475c170e9683bf365ec044",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_sha256(path: Path, expected: str, *, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing {label}: {path}")
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(
            f"unexpected {label} for {path}: expected {expected}, got {actual}"
        )


def assert_absent(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"unexpected existing runtime target: {path}")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one matching block in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def install_exact_source(*, source_root: Path, relative_path: str) -> Path:
    source = source_root / relative_path
    target = APP_ROOT / relative_path
    expected = EXPECTED_SOURCE_SHA256[relative_path]
    assert_sha256(source, expected, label="staged hotfix source")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    assert_sha256(target, expected, label="installed hotfix source")
    return target


def patch_reviews_for_stable_runtime(path: Path) -> None:
    replace_once(
        path,
        "from forwin.candidate_drafts import CandidateDraftRepository\n"
        "from forwin.api_schema import (\n",
        "from forwin.candidate_drafts import CandidateDraftRepository\n"
        "from forwin.canon.historical_rewrite import (\n"
        "    HistoricalCanonRewriteRepository,\n"
        "    HistoricalRewriteInvalid,\n"
        ")\n"
        "from forwin.api_schema import (\n",
    )
    replace_once(
        path,
        "    continue_requested_chapters = 0\n"
        "    session = get_session()\n"
        "    try:\n"
        "        project = session.get(Project, project_id)\n",
        "    continue_requested_chapters = 0\n"
        "    session = get_session()\n"
        "    try:\n"
        "        project = session.execute(\n"
        "            select(Project).where(Project.id == project_id).with_for_update()\n"
        "        ).scalar_one_or_none()\n",
    )
    replace_once(
        path,
        "        plan.status = \"planned\"\n",
        "        if previous_status == \"accepted\":\n"
        "            try:\n"
        "                marker = HistoricalCanonRewriteRepository(session).mark_pending(\n"
        "                    project_id=project_id,\n"
        "                    chapter_number=chapter_number,\n"
        "                    reason=reason,\n"
        "                )\n"
        "                marker.related_object_id = str(plan.id)\n"
        "            except HistoricalRewriteInvalid as exc:\n"
        "                raise HTTPException(409, str(exc)) from exc\n"
        "        plan.status = \"planned\"\n",
    )
    replace_once(
        path,
        "        log_decision_event(\n"
        "            session,\n"
        "            project_id=project_id,\n"
        "            event_family=\"audit_action\",\n"
        "            event_type=DecisionEventType.RETRY_ATTEMPT,\n"
        "            actor_type=\"api\",\n"
        "            scope=\"chapter\",\n"
        "            summary=f\"第{chapter_number}章 review 候选已重置为 planned，等待重写。\",\n"
        "            reason=reason,\n"
        "            payload={\n"
        "                \"chapter_number\": chapter_number,\n"
        "                \"previous_status\": previous_status,\n"
        "            },\n"
        "            chapter_number=chapter_number,\n"
        "            related_object_type=\"chapter\",\n"
        "            related_object_id=str(plan.id),\n"
        "        )\n",
        "        if previous_status != \"accepted\":\n"
        "            log_decision_event(\n"
        "                session,\n"
        "                project_id=project_id,\n"
        "                event_family=\"audit_action\",\n"
        "                event_type=DecisionEventType.RETRY_ATTEMPT,\n"
        "                actor_type=\"api\",\n"
        "                scope=\"chapter\",\n"
        "                summary=f\"第{chapter_number}章 review 候选已重置为 planned，等待重写。\",\n"
        "                reason=reason,\n"
        "                payload={\n"
        "                    \"chapter_number\": chapter_number,\n"
        "                    \"previous_status\": previous_status,\n"
        "                },\n"
        "                chapter_number=chapter_number,\n"
        "                related_object_type=\"chapter\",\n"
        "                related_object_id=str(plan.id),\n"
        "            )\n",
    )


def patch_admission_for_stable_runtime(path: Path) -> None:
    replace_once(
        path,
        "from .entity_admission import EntityAdmissionCommitter\n"
        "from .plan import CanonCommitPlan\n",
        "from .entity_admission import EntityAdmissionCommitter\n"
        "from .historical_rewrite import HistoricalCanonRewriteService, HistoricalRewriteInvalid\n"
        "from .plan import CanonCommitPlan\n",
    )
    replace_once(
        path,
        "                if prior is not None:\n"
        "                    return _outcome_from_record(prior, idempotent=True)\n",
        "                if prior is not None:\n"
        "                    if prior.status == \"superseded\":\n"
        "                        raise CanonStaleVersion(\"Canon commit has been superseded\")\n"
        "                    return _outcome_from_record(prior, idempotent=True)\n",
    )
    replace_once(
        path,
        "                self._revalidate_locked_plan(\n"
        "                    session=session,\n"
        "                    project=project,\n"
        "                    chapter=chapter,\n"
        "                    candidate=candidate,\n"
        "                    plan=plan,\n"
        "                )\n",
        "                rewrite = None\n"
        "                try:\n"
        "                    if chapter is not None and chapter.status != \"accepted\":\n"
        "                        rewrite = HistoricalCanonRewriteService(session).prepare_replacement(\n"
        "                            plan\n"
        "                        )\n"
        "                except HistoricalRewriteInvalid as exc:\n"
        "                    raise CanonStaleVersion(str(exc)) from exc\n"
        "                if rewrite is not None:\n"
        "                    rewrite.retire_old_contribution()\n"
        "                self._revalidate_locked_plan(\n"
        "                    session=session,\n"
        "                    project=project,\n"
        "                    chapter=chapter,\n"
        "                    candidate=candidate,\n"
        "                    plan=plan,\n"
        "                    retained_chapter_delta_ids=frozenset(rewrite.retained_delta_ids)\n"
        "                    if rewrite\n"
        "                    else frozenset(),\n"
        "                )\n",
    )
    replace_once(
        path,
        "                compile_result = BookStateCompiler(session).compile(\n",
        "                compiler = BookStateCompiler(session)\n"
        "                compile_result = compiler.compile(\n",
    )
    replace_once(
        path,
        "                if compile_result.metadata.get(\"idempotent\"):\n"
        "                    raise CanonStaleVersion(\n"
        "                        \"BookState deltas already exist without a Canon commit record\"\n"
        "                    )\n"
        "                session.flush()\n",
        "                if compile_result.metadata.get(\"idempotent\"):\n"
        "                    raise CanonStaleVersion(\n"
        "                        \"BookState deltas already exist without a Canon commit record\"\n"
        "                    )\n"
        "                if rewrite is not None:\n"
        "                    compile_result = rewrite.rebuild_successor_projections(\n"
        "                        compile_result, compiler=compiler\n"
        "                    )\n"
        "                session.flush()\n",
    )
    replace_once(
        path,
        "                result_payload = {\n"
        "                    \"commit_id\": commit_id,\n"
        "                    \"compile_result\": compile_result.model_dump(mode=\"json\"),\n"
        "                }\n",
        "                result_payload = {\n"
        "                    \"commit_id\": commit_id,\n"
        "                    \"compile_result\": compile_result.model_dump(mode=\"json\"),\n"
        "                }\n"
        "                if rewrite is not None:\n"
        "                    rewrite.mark_prior_commit_superseded(\n"
        "                        replacement_commit_id=commit_id\n"
        "                    )\n"
        "                    result_payload[\"historical_rewrite\"] = {\n"
        "                        \"superseded_commit_id\": rewrite.previous.id,\n"
        "                        \"retired_delta_ids\": rewrite.retired_delta_ids,\n"
        "                        \"replayed_chapters\": rewrite.replayed_chapters,\n"
        "                    }\n",
    )
    replace_once(
        path,
        "        plan: CanonCommitPlan,\n"
        "    ) -> None:\n",
        "        plan: CanonCommitPlan,\n"
        "        retained_chapter_delta_ids: frozenset[str] = frozenset(),\n"
        "    ) -> None:\n",
    )
    replace_once(
        path,
        "        current_chapter_delta_count = int(\n"
        "            session.scalar(\n"
        "                select(func.count(GraphDeltaRow.id)).where(\n"
        "                    GraphDeltaRow.project_id == plan.project_id,\n"
        "                    GraphDeltaRow.chapter_number == plan.chapter_number,\n"
        "                )\n"
        "            )\n"
        "            or 0\n"
        "        )\n"
        "        if current_chapter_delta_count:\n"
        "            raise CanonStaleVersion(\"BookState already contains this chapter\")\n",
        "        current_chapter_delta_ids = set(\n"
        "            session.scalars(\n"
        "                select(GraphDeltaRow.id).where(\n"
        "                    GraphDeltaRow.project_id == plan.project_id,\n"
        "                    GraphDeltaRow.chapter_number == plan.chapter_number,\n"
        "                )\n"
        "            )\n"
        "        )\n"
        "        if current_chapter_delta_ids != retained_chapter_delta_ids:\n"
        "            raise CanonStaleVersion(\"BookState already contains this chapter\")\n",
    )


def patch_historical_rewrite_for_stable_plan(path: Path) -> None:
    replace_once(
        path,
        "    def mark_prior_commit_superseded(self) -> None:\n"
        "        # Project locking in admission serializes allocation. Keep the original\n"
        "        # chapter in the audit result while freeing the existing unique key.\n"
        "        minimum = self.session.scalar(\n"
        "            select(func.min(CanonCommitRecord.chapter_number)).where(\n"
        "                CanonCommitRecord.project_id == self.plan.project_id,\n"
        "            )\n"
        "        )\n"
        "        archive_chapter = min(int(minimum or 0), 0) - 1\n"
        "        result = json.loads(self.previous.result_json or \"{}\")\n"
        "        result.update(\n"
        "            {\n"
        "                \"original_chapter_number\": self.previous.chapter_number,\n"
        "                \"superseded_by_commit_id\": self.plan.canon_commit_id,\n"
        "                \"replayed_chapters\": self.replayed_chapters,\n"
        "            }\n"
        "        )\n"
        "        self.previous.result_json = json.dumps(\n"
        "            result, ensure_ascii=False, sort_keys=True\n"
        "        )\n"
        "        self.previous.chapter_number = archive_chapter\n"
        "        self.previous.status = \"superseded\"\n"
        "        payload = _payload(self.marker)\n"
        "        payload[\"replacement_commit_id\"] = self.plan.canon_commit_id\n"
        "        self.marker.payload_json = json.dumps(\n"
        "            payload, ensure_ascii=False, sort_keys=True\n"
        "        )\n"
        "        self.session.flush()\n",
        "    def mark_prior_commit_superseded(self, *, replacement_commit_id: str) -> None:\n"
        "        replacement_id = str(replacement_commit_id or \"\").strip()\n"
        "        if not replacement_id:\n"
        "            raise HistoricalRewriteInvalid(\"historical rewrite replacement ID missing\")\n"
        "        minimum = self.session.scalar(\n"
        "            select(func.min(CanonCommitRecord.chapter_number)).where(\n"
        "                CanonCommitRecord.project_id == self.plan.project_id,\n"
        "            )\n"
        "        )\n"
        "        archive_chapter = min(int(minimum or 0), 0) - 1\n"
        "        result = json.loads(self.previous.result_json or \"{}\")\n"
        "        result.update(\n"
        "            {\n"
        "                \"original_chapter_number\": self.previous.chapter_number,\n"
        "                \"superseded_by_commit_id\": replacement_id,\n"
        "                \"replayed_chapters\": self.replayed_chapters,\n"
        "            }\n"
        "        )\n"
        "        self.previous.result_json = json.dumps(\n"
        "            result, ensure_ascii=False, sort_keys=True\n"
        "        )\n"
        "        self.previous.chapter_number = archive_chapter\n"
        "        self.previous.status = \"superseded\"\n"
        "        payload = _payload(self.marker)\n"
        "        payload[\"replacement_commit_id\"] = replacement_id\n"
        "        self.marker.payload_json = json.dumps(\n"
        "            payload, ensure_ascii=False, sort_keys=True\n"
        "        )\n"
        "        self.session.flush()\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args()
    source_root = args.source_root
    if not source_root.is_dir():
        raise RuntimeError(f"missing staged hotfix source root: {source_root}")
    for relative_path, expected in EXPECTED_BASE_SHA256.items():
        assert_sha256(APP_ROOT / relative_path, expected, label="runtime base")
    for relative_path, expected in EXPECTED_COMPATIBILITY_SENTINEL_SHA256.items():
        assert_sha256(APP_ROOT / relative_path, expected, label="compatibility base")
    assert_absent(APP_ROOT / "forwin/canon/historical_rewrite.py")

    historical = install_exact_source(
        source_root=source_root,
        relative_path="forwin/canon/historical_rewrite.py",
    )
    repository = install_exact_source(
        source_root=source_root,
        relative_path="forwin/book_state/repository.py",
    )
    runtime = install_exact_source(
        source_root=source_root,
        relative_path="forwin/book_state/runtime.py",
    )
    projection = install_exact_source(
        source_root=source_root,
        relative_path="forwin/book_state/projection.py",
    )
    reviews = APP_ROOT / "forwin/application/projects/reviews.py"
    admission = APP_ROOT / "forwin/canon/admission.py"
    patch_historical_rewrite_for_stable_plan(historical)
    patch_reviews_for_stable_runtime(reviews)
    patch_admission_for_stable_runtime(admission)
    for target in (historical, admission, reviews, repository, runtime, projection):
        py_compile.compile(str(target), doraise=True)


if __name__ == "__main__":
    main()
