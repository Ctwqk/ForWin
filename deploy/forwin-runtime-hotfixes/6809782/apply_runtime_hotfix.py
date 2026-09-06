from __future__ import annotations

import argparse
import hashlib
import py_compile
from pathlib import Path


APP_ROOT = Path("/app")

# Existing files must be exactly the stable compat-3738779 runtime before mutation.
EXPECTED_BASE_SHA256 = {
    "forwin/canon/admission.py": "80fd1add05cb1a6e96cac4775b1baeeb644878348a2dcd72394bff9df340e768",
    "forwin/application/projects/reviews.py": "121f436557880a9a90249227b9552e4b52efc658f8eba13189238a117dc64832",
    "forwin/book_state/repository.py": "1a187d416403568eeb54791c617595f604419cb527586a38f27d230c5dc97e04",
    "forwin/book_state/runtime.py": "0735b09e5272ae5530b6ee709ab52b027977b8ab8620c32c4bedd077296dd2cc",
    "forwin/book_state/projection.py": "5c9cc52c82358f1f1546a5a624ac43986443b9795bfd8ba33a1757595b787d48",
    "forwin/generation/pipeline_core/acceptance.py": "c5bc08a4bc88d34a736fdff51b84049134588c37c059e48441ca040fc1951606",
    "forwin/outbox/store.py": "9d7a4886cfb13704468ee4fb05c5c6304a1b04904c55263f523f13acce8dfb51",
}
EXPECTED_COMPATIBILITY_SENTINEL_SHA256 = {
    "forwin/context/assembler_core/canon_quality_context.py": "9c8520d3f7e6b0849a252e34250d6e4362cc5c786e7be641cd6f5b88a6e73050",
    "forwin/writer/prompt_core/constraints.py": "196b659a6e04f049c6372a721a462cae7860be5f4103f80a4c7ef3b2f9259c84",
    "forwin/candidate_drafts.py": "3c8269bda5839207a175b4325c65f7b95824277233339169a44350d38c59ea0d",
    "forwin/naming/entity_registrar.py": "3176f884c4fd1b8ab5ce37c3022c76de227612938387450fb851db6929c25d1a",
    "forwin/protocol/writer.py": "a67e88a636535734ab10839039cd9dcdd339adbe75714c2d47ce6b95935cb748",
    "forwin/models/draft.py": "a4ead827008b57676fa4a2a0ba540ed54360a352737107658e724f784088382c",
}
# Stable-compatible inputs are from 4c1d32d; exact transforms below mirror the
# reviewer-approved historical reapproval evidence semantics in 24a477b.
EXPECTED_SOURCE_SHA256 = {
    "forwin/canon/historical_rewrite.py": "38fb90b913e1f82300b9ce771240180f4a174fea8dd4adce38f7c638f05009c8",
    "forwin/canon/review_recovery.py": "c2a1e2da24cf11d1053cc3ce110ee5fca621cf5c5e516fe65d093b3912d23cbd",
    "forwin/book_state/repository.py": "29a33864c5db1047c32b6a5b92840ebece8e0793123b94fbaec41d9a7772e60b",
    "forwin/book_state/runtime.py": "e494a1eade394ea6313794ccc6f281c3e100b7b2398d9402b445e21cd3c72be0",
    "forwin/book_state/projection.py": "b84ebb60539e46525d72004b981ee4596c74023560475c170e9683bf365ec044",
}
# Every installed target is verified after stable adaptation and before compilation.
EXPECTED_FINAL_SHA256 = {
    "forwin/canon/historical_rewrite.py": "ee9752dc38451b8662c91a5971e0a9af7f871d5841a46396e18021f6facb265a",
    "forwin/canon/review_recovery.py": "09be62d3cf67ceb9d6efe1554f6914e67db57ab680838c94e5b0ac820bf265bb",
    "forwin/canon/admission.py": "289704ad11dd805d2aa47cf9e8d7e02ea1175a0389a9e2f2fee0a6c613ac4cbf",
    "forwin/application/projects/reviews.py": "77e356cb6c0ef00230ccde3605d7b1e73deaf2ba11ce41064d2c9b274bc12d13",
    "forwin/book_state/repository.py": "29a33864c5db1047c32b6a5b92840ebece8e0793123b94fbaec41d9a7772e60b",
    "forwin/book_state/runtime.py": "e494a1eade394ea6313794ccc6f281c3e100b7b2398d9402b445e21cd3c72be0",
    "forwin/book_state/projection.py": "b84ebb60539e46525d72004b981ee4596c74023560475c170e9683bf365ec044",
    "forwin/generation/pipeline_core/acceptance.py": "105169b35676b06e65f8b3ea608dadc231ea4e3bd96030c38302946f8e758240",
    "forwin/outbox/store.py": "b18fd648e5c63a3dc94dd73e65fc197d0e100c265f334b3f071feb95ac2d845b",
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


def assert_not_contains(path: Path, text: str, *, label: str) -> None:
    if text in path.read_text():
        raise RuntimeError(f"unexpected {label} dependency in {path}: {text}")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one matching block in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def verify_staged_source_manifest(source_root: Path) -> None:
    for relative_path, expected in sorted(EXPECTED_SOURCE_SHA256.items()):
        assert_sha256(
            source_root / relative_path,
            expected,
            label="staged hotfix source",
        )


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
    replace_once(
        path,
        "        if \"reason\" in accept_review_parameters:\n"
        "            result = pipeline.accept_review(project_id, chapter_number, reason=reason)\n"
        "        else:\n"
        "            result = pipeline.accept_review(project_id, chapter_number)\n",
        "        accept_kwargs = {}\n"
        "        if \"reason\" in accept_review_parameters:\n"
        "            accept_kwargs[\"reason\"] = reason\n"
        "        if \"actor_type\" in accept_review_parameters:\n"
        "            accept_kwargs[\"actor_type\"] = \"api\"\n"
        "        if \"source\" in accept_review_parameters:\n"
        "            accept_kwargs[\"source\"] = \"chapter_review_approve_api\"\n"
        "        result = pipeline.accept_review(\n"
        "            project_id,\n"
        "            chapter_number,\n"
        "            **accept_kwargs,\n"
        "        )\n",
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


def patch_review_recovery_for_stable_runtime(path: Path) -> None:
    replace_once(
        path,
        "from forwin.candidate_drafts import (\n"
        "    CandidateDraftRepository,\n"
        "    CandidateTransitionError,\n"
        "    candidate_body_hash,\n"
        "    candidate_plan_revision,\n"
        "    candidate_writer_output_admission_fingerprint,\n"
        ")\n",
        "from forwin.candidate_drafts import (\n"
        "    CandidateDraftRepository,\n"
        "    CandidateTransitionError,\n"
        "    candidate_body_hash,\n"
        "    candidate_plan_revision,\n"
        ")\n",
    )
    replace_once(
        path,
        "from forwin.outbox.store import any_outbox_events_exist\n\n"
        "from .historical_rewrite import HistoricalCanonRewriteService\n",
        "from forwin.naming.entity_registrar import writer_output_admission_fingerprint\n"
        "from forwin.outbox.store import any_outbox_events_exist\n\n"
        "\n"
        "_ADMISSION_FINGERPRINT_KEY = \"writer_output_admission_fingerprint\"\n"
        "\n"
        "\n"
        "def _stable_candidate_metadata(\n"
        "    candidate: CandidateDraftRecord,\n"
        ") -> dict[str, object] | None:\n"
        "    try:\n"
        "        metadata = json.loads(str(candidate.metadata_json or \"{}\"))\n"
        "    except (TypeError, json.JSONDecodeError):\n"
        "        return None\n"
        "    if not isinstance(metadata, dict):\n"
        "        return None\n"
        "    return metadata\n"
        "\n"
        "\n"
        "def _stable_writer_output_binds_recovery(\n"
        "    candidate: CandidateDraftRecord,\n"
        "    draft: ChapterDraft,\n"
        "    writer_output: object,\n"
        "    *,\n"
        "    project_id: str,\n"
        "    chapter_number: int,\n"
        ") -> bool:\n"
        "    metadata = _stable_candidate_metadata(candidate)\n"
        "    if metadata is None:\n"
        "        return False\n"
        "    if str(getattr(writer_output, \"project_id\", \"\") or \"\") != project_id:\n"
        "        return False\n"
        "    if int(getattr(writer_output, \"chapter_number\", 0) or 0) != chapter_number:\n"
        "        return False\n"
        "    body = str(getattr(writer_output, \"body\", \"\") or \"\")\n"
        "    if body != str(draft.body_text or \"\"):\n"
        "        return False\n"
        "    if candidate_body_hash(body) != str(candidate.body_hash or \"\"):\n"
        "        return False\n"
        "    title = metadata.get(\"title\")\n"
        "    if (\n"
        "        not isinstance(title, str)\n"
        "        or not title\n"
        "        or title != str(getattr(writer_output, \"title\", \"\") or \"\")\n"
        "    ):\n"
        "        return False\n"
        "    return True\n"
        "\n"
        "\n"
        "def _stable_recovery_fingerprint_state(\n"
        "    candidate: CandidateDraftRecord,\n"
        "    *,\n"
        "    expected_writer_output_fingerprint: str,\n"
        "    expected_plan_fingerprint: str,\n"
        ") -> tuple[bool, dict[str, object] | None]:\n"
        "    metadata = _stable_candidate_metadata(candidate)\n"
        "    writer_fingerprint = str(expected_writer_output_fingerprint or \"\").strip()\n"
        "    plan_fingerprint = str(expected_plan_fingerprint or \"\").strip()\n"
        "    if (\n"
        "        metadata is None\n"
        "        or not writer_fingerprint\n"
        "        or not plan_fingerprint\n"
        "        or writer_fingerprint != plan_fingerprint\n"
        "    ):\n"
        "        return False, None\n"
        "    if _ADMISSION_FINGERPRINT_KEY in metadata:\n"
        "        stored = metadata[_ADMISSION_FINGERPRINT_KEY]\n"
        "        return (\n"
        "            isinstance(stored, str) and bool(stored) and stored == plan_fingerprint,\n"
        "            None,\n"
        "        )\n"
        "    metadata[_ADMISSION_FINGERPRINT_KEY] = writer_fingerprint\n"
        "    return True, metadata\n"
        "\n"
        "\n"
        "from .historical_rewrite import HistoricalCanonRewriteService\n",
    )
    replace_once(
        path,
        "    review_id: str,\n"
        ") -> None:\n",
        "    review_id: str,\n"
        "    actor_type: str,\n"
        "    source: str,\n"
        "    writer_output: object,\n"
        "    artifact_path: str,\n"
        ") -> None:\n",
    )
    replace_once(
        path,
        "    error = \"failed candidate is not an authorized historical re-review\"\n"
        "    project = session.scalar(\n",
        "    error = \"failed candidate is not an authorized historical re-review\"\n"
        "    if actor_type != \"api\" or source != \"chapter_review_approve_api\":\n"
        "        raise CandidateTransitionError(error)\n"
        "    project = session.scalar(\n",
    )
    replace_once(
        path,
        "    try:\n"
        "        plan = CanonCommitPlan.model_validate_json(candidate.canon_commit_plan_json)\n"
        "        eligibility = json.loads(candidate.eligibility_decision_json)\n"
        "        if not isinstance(eligibility, dict):\n"
        "            raise ValueError(error)\n"
        "    except (TypeError, ValueError) as exc:\n"
        "        raise CandidateTransitionError(error) from exc\n"
        "    draft = session.scalar(\n",
        "    try:\n"
        "        plan_payload = json.loads(candidate.canon_commit_plan_json)\n"
        "        if not isinstance(plan_payload, dict) or plan_payload.get(\n"
        "            \"acceptance_mode\"\n"
        "        ) not in {\"normal\", \"human_approved\"}:\n"
        "            raise ValueError(error)\n"
        "        plan = CanonCommitPlan.model_validate_json(candidate.canon_commit_plan_json)\n"
        "        eligibility = json.loads(candidate.eligibility_decision_json)\n"
        "        metadata = _stable_candidate_metadata(candidate)\n"
        "        if not isinstance(eligibility, dict) or metadata is None:\n"
        "            raise ValueError(error)\n"
        "    except (TypeError, ValueError, json.JSONDecodeError) as exc:\n"
        "        raise CandidateTransitionError(error) from exc\n"
        "    draft = session.scalar(\n",
    )
    replace_once(
        path,
        "        or plan.acceptance_mode != \"human_approved\"\n",
        "        or plan.acceptance_mode not in {\"normal\", \"human_approved\"}\n",
    )
    replace_once(
        path,
        "        or candidate_writer_output_admission_fingerprint(candidate)\n"
        "        != plan.entity_admission_plan.candidate_fingerprint\n",
        "",
    )
    replace_once(
        path,
        "        or candidate.project_id != project_id\n"
        "        or plan.project_id != project_id\n",
        "        or candidate.project_id != project_id\n"
        "        or not isinstance(artifact_path, str)\n"
        "        or not artifact_path.strip()\n"
        "        or draft.llm_raw_response != artifact_path\n"
        "        or candidate.writer_artifact_ref != artifact_path\n"
        "        or not _stable_writer_output_binds_recovery(\n"
        "            candidate,\n"
        "            draft,\n"
        "            writer_output,\n"
        "            project_id=project_id,\n"
        "            chapter_number=chapter_number,\n"
        "        )\n"
        "        or plan.project_id != project_id\n",
    )
    replace_once(
        path,
        "    if session.scalar(\n"
        "        select(CanonCommitRecord.id).where(\n",
        "    try:\n"
        "        artifact_fingerprint = writer_output_admission_fingerprint(writer_output)\n"
        "    except (AttributeError, TypeError, ValueError) as exc:\n"
        "        raise CandidateTransitionError(error) from exc\n"
        "    fingerprint_matches, metadata_backfill = _stable_recovery_fingerprint_state(\n"
        "        candidate,\n"
        "        expected_writer_output_fingerprint=artifact_fingerprint,\n"
        "        expected_plan_fingerprint=plan.entity_admission_plan.candidate_fingerprint,\n"
        "    )\n"
        "    if not fingerprint_matches:\n"
        "        raise CandidateTransitionError(error)\n"
        "    if session.scalar(\n"
        "        select(CanonCommitRecord.id).where(\n",
    )
    replace_once(
        path,
        "    # This exception to failed's terminal state exists only at manual re-review.\n"
        "    # Keep the prior failure reason and plan until normal preparation succeeds.\n"
        "    candidate.status = \"needs_review\"\n"
        "    session.flush()\n",
        "    # Backfill only the genuinely absent immutable fingerprint after every\n"
        "    # recovery guard, including strict marker chronology, has succeeded.\n"
        "    if metadata_backfill is not None:\n"
        "        candidate.metadata_json = json.dumps(\n"
        "            metadata_backfill, ensure_ascii=False, sort_keys=True\n"
        "        )\n"
        "    # This exception to failed's terminal state exists only at manual re-review.\n"
        "    # Keep the prior failure reason and plan until normal preparation succeeds.\n"
        "    candidate.status = \"needs_review\"\n"
        "    session.flush()\n",
    )


def patch_acceptance_for_stable_runtime(path: Path) -> None:
    replace_once(
        path,
        "from forwin.candidate_drafts import CandidateDraftRepository\n"
        "from forwin.generation.pipeline_core.obligation_resolution import (\n",
        "from forwin.candidate_drafts import CandidateDraftRepository\n"
        "from forwin.canon.review_recovery import (\n"
        "    reopen_failed_historical_candidate_for_review,\n"
        ")\n"
        "from forwin.generation.pipeline_core.obligation_resolution import (\n",
    )
    replace_once(
        path,
        "        *,\n"
        "        reason: str = \"\",\n"
        "    ) -> dict[str, str]:\n",
        "        *,\n"
        "        reason: str = \"\",\n"
        "        actor_type: str = \"compat_direct\",\n"
        "        source: str = \"compat_direct\",\n"
        "    ) -> dict[str, str]:\n",
    )
    replace_once(
        path,
        "            if candidate is None or candidate.candidate_draft_id != latest_draft.id:\n"
        "                raise ValueError(f\"第{chapter_number}章缺少 v5 candidate record\")\n"
        "\n"
        "            writer_output = self._load_writer_output_from_meta(\n",
        "            if candidate is None or candidate.candidate_draft_id != latest_draft.id:\n"
        "                raise ValueError(f\"第{chapter_number}章缺少 v5 candidate record\")\n"
        "\n"
        "            artifact_path = latest_draft.llm_raw_response\n"
        "            writer_output = self._load_writer_output_from_meta(\n",
    )
    replace_once(
        path,
        "            writer_output = self._load_writer_output_from_meta(\n"
        "                latest_draft.llm_raw_response\n"
        "            )\n"
        "            verdict = self._load_review_verdict(latest_review)\n",
        "            writer_output = self._load_writer_output_from_meta(\n"
        "                artifact_path\n"
        "            )\n"
        "            if candidate.status == \"failed\":\n"
        "                reopen_failed_historical_candidate_for_review(\n"
        "                    session,\n"
        "                    project_id=project_id,\n"
        "                    chapter_number=chapter_number,\n"
        "                    candidate_id=candidate.id,\n"
        "                    draft_id=latest_draft.id,\n"
        "                    review_id=latest_review.id,\n"
        "                    actor_type=actor_type,\n"
        "                    source=source,\n"
        "                    writer_output=writer_output,\n"
        "                    artifact_path=artifact_path,\n"
        "                )\n"
        "            verdict = self._load_review_verdict(latest_review)\n",
    )


def patch_outbox_for_stable_runtime(path: Path) -> None:
    replace_once(
        path,
        "import json\n"
        "from datetime import datetime, timedelta, timezone\n",
        "import json\n"
        "from collections.abc import Sequence\n"
        "from datetime import datetime, timedelta, timezone\n",
    )
    replace_once(
        path,
        "def utcnow() -> datetime:\n"
        "    return datetime.now(timezone.utc)\n\n\n"
        "def enqueue_outbox_event(\n",
        "def utcnow() -> datetime:\n"
        "    return datetime.now(timezone.utc)\n\n\n"
        "def any_outbox_events_exist(session: Session, *, event_ids: Sequence[str]) -> bool:\n"
        "    \"\"\"Read stable event identity without exposing outbox rows to other owners.\"\"\"\n"
        "    return bool(\n"
        "        session.scalar(\n"
        "            select(\n"
        "                select(OutboxEvent.id)\n"
        "                .where(OutboxEvent.event_id.in_(event_ids))\n"
        "                .exists()\n"
        "            )\n"
        "        )\n"
        "    )\n\n\n"
        "def enqueue_outbox_event(\n",
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
    assert_absent(APP_ROOT / "forwin/canon/review_recovery.py")
    assert_absent(APP_ROOT / "forwin/application/read_models")
    assert_not_contains(
        APP_ROOT / "forwin/candidate_drafts.py",
        "candidate_writer_output_admission_fingerprint",
        label="source-only candidate fingerprint",
    )
    # Validate every staged input before the first target write.
    verify_staged_source_manifest(source_root)

    historical = install_exact_source(
        source_root=source_root,
        relative_path="forwin/canon/historical_rewrite.py",
    )
    review_recovery = install_exact_source(
        source_root=source_root,
        relative_path="forwin/canon/review_recovery.py",
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
    acceptance = APP_ROOT / "forwin/generation/pipeline_core/acceptance.py"
    outbox = APP_ROOT / "forwin/outbox/store.py"
    patch_historical_rewrite_for_stable_plan(historical)
    patch_review_recovery_for_stable_runtime(review_recovery)
    patch_reviews_for_stable_runtime(reviews)
    patch_admission_for_stable_runtime(admission)
    patch_acceptance_for_stable_runtime(acceptance)
    patch_outbox_for_stable_runtime(outbox)
    assert_not_contains(
        historical,
        "self.plan.canon_commit_id",
        label="source-only Canon commit ID",
    )
    assert_not_contains(
        review_recovery,
        "candidate_writer_output_admission_fingerprint",
        label="source-only candidate fingerprint",
    )
    for target in (historical, review_recovery, reviews, admission, acceptance, outbox):
        assert_not_contains(
            target,
            "forwin.application.read_models",
            label="source-only read-model",
        )
    installed = {
        "forwin/canon/historical_rewrite.py": historical,
        "forwin/canon/review_recovery.py": review_recovery,
        "forwin/canon/admission.py": admission,
        "forwin/application/projects/reviews.py": reviews,
        "forwin/book_state/repository.py": repository,
        "forwin/book_state/runtime.py": runtime,
        "forwin/book_state/projection.py": projection,
        "forwin/generation/pipeline_core/acceptance.py": acceptance,
        "forwin/outbox/store.py": outbox,
    }
    # Verify every transformed and copied output before Python compilation.
    for relative_path, target in installed.items():
        assert_sha256(
            target,
            EXPECTED_FINAL_SHA256[relative_path],
            label="final installed hotfix",
        )
    for target in installed.values():
        py_compile.compile(str(target), doraise=True)


if __name__ == "__main__":
    main()
