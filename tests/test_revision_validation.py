"""A full suffix, fresh body extraction and complete coverage are mandatory."""

import hashlib

import pytest


def _manifest():
    from forwin.canon.revision_validation import RevisionChapterInput, RevisionManifest

    return RevisionManifest(
        project_id="project",
        base_book_revision=3,
        from_chapter=2,
        through_chapter=3,
        policy_fingerprint="policy",
        model_identity={"model": "frozen-model", "provider": "local-test"},
        chapters=tuple(
            RevisionChapterInput(
                chapter_plan_id=f"chapter-{number}",
                chapter_number=number,
                base_commit_id=f"accepted-{number}",
                candidate_id=f"candidate-{number}",
                draft_id=f"draft-{number}",
                title=f"Chapter {number}",
                body=f"Body {number}.",
                body_sha256=hashlib.sha256(f"Body {number}.".encode()).hexdigest(),
            )
            for number in (2, 3)
        ),
    )


def _covered(manifest, *, failed="", omitted=""):
    from forwin.canon.revision_validation import (
        REQUIRED_REVISION_CHECKS,
        RevisionChapterAssessment,
        RevisionCheck,
    )

    return tuple(
        RevisionChapterAssessment(
            chapter_number=chapter.chapter_number,
            body_sha256=chapter.body_sha256,
            checks=tuple(
                RevisionCheck(
                    dimension=dimension,
                    status="fail" if dimension == failed else "pass",
                    evidence_refs=(f"body:{chapter.body_sha256}",),
                    explanation="Entire body checked against candidate prefix",
                )
                for dimension in REQUIRED_REVISION_CHECKS
                if dimension != omitted
            ),
        )
        for chapter in manifest.chapters
    )


def test_coverage_cannot_claim_pass_after_omitting_any_successor():
    from forwin.canon.revision_validation import assess_revision

    manifest = _manifest()
    result = assess_revision(manifest, _covered(manifest)[:1])
    assert result.status == "unknown"
    assert "chapter:3" in " ".join(result.uncovered)


@pytest.mark.parametrize(
    "dimension",
    [
        "body_extraction",
        "knowledge",
        "life_state",
        "time",
        "place",
        "obligations",
        "possession",
    ],
)
def test_critical_dimension_cannot_be_omitted_or_substituted_by_replay(dimension):
    from forwin.canon.revision_validation import assess_revision

    manifest = _manifest()
    result = assess_revision(manifest, _covered(manifest, omitted=dimension))
    assert result.status == "unknown"
    assert dimension in " ".join(result.uncovered)


@pytest.mark.parametrize(
    "dimension",
    ["knowledge", "life_state", "time", "place", "obligations", "possession"],
)
def test_known_hard_conflict_rejects_complete_suffix(dimension):
    from forwin.canon.revision_validation import assess_revision

    manifest = _manifest()
    assert (
        assess_revision(manifest, _covered(manifest, failed=dimension)).status == "fail"
    )


def test_validation_identity_binds_every_body_and_model_without_credentials():
    from forwin.canon.revision_validation import RevisionManifest, assess_revision

    manifest = _manifest()
    first = assess_revision(manifest, _covered(manifest))
    assert first.status == "pass"
    with pytest.raises(ValueError, match="credential|model identity"):
        RevisionManifest.model_validate(
            {
                **manifest.model_dump(),
                "model_identity": {"model": "frozen-model", "api_key": "secret"},
            }
        )
    changed = manifest.model_copy(update={"base_book_revision": 4})
    assert (
        assess_revision(changed, _covered(changed)).validation_id != first.validation_id
    )


def test_manifest_requires_every_sequence_and_verified_body_hash():
    from forwin.canon.revision_validation import RevisionManifest

    manifest = _manifest()
    with pytest.raises(ValueError, match="complete|contiguous"):
        RevisionManifest.model_validate(
            {**manifest.model_dump(), "chapters": manifest.chapters[:1]}
        )
    changed = manifest.chapters[0].model_dump()
    changed["body"] = "Unreviewed edit"
    with pytest.raises(ValueError, match="body hash"):
        RevisionManifest.model_validate(
            {**manifest.model_dump(), "chapters": [changed, manifest.chapters[1]]}
        )


def test_timeout_records_remaining_range_as_unknown_instead_of_shortened_pass():
    from forwin.canon.revision_validation import validate_chapter_sequence

    manifest = _manifest()
    seen = []

    def evaluate(chapter):
        seen.append(chapter.chapter_number)
        raise TimeoutError("model deadline")

    result = validate_chapter_sequence(manifest, evaluate=evaluate)
    assert result.status == "unknown"
    assert seen == [2]
    assert "chapter:3" in " ".join(result.uncovered)
    assert "timeout" in " ".join(result.uncovered).lower()


def test_wording_only_request_still_runs_every_body_and_never_reuses_old_review():
    from forwin.canon.revision_validation import validate_chapter_sequence

    manifest = _manifest()
    expected = _covered(manifest)
    seen = []

    def evaluate(chapter):
        seen.append((chapter.chapter_number, chapter.body))
        return expected[chapter.chapter_number - manifest.from_chapter]

    result = validate_chapter_sequence(manifest, evaluate=evaluate)
    assert result.status == "pass"
    assert seen == [(2, "Body 2."), (3, "Body 3.")]
