"""Historical coverage requirements for critical items in the existing form.

This verifies whether an item was answered, not whether Canon may admit it.
The existing evidence validator/projector continues to own factual transitions.
"""

from __future__ import annotations

from .form_schema import ChapterReviewAnswers, ChapterReviewForm, FormAnswer
from .historical_schema import HISTORICAL_DIMENSIONS
from .pruning import BLOCKING_SIGNAL_SEVERITIES, OPEN_COUNTDOWN_STATUSES


def missing_tracked_coverage(
    *,
    form: ChapterReviewForm,
    answers: ChapterReviewAnswers,
    body: str,
    min_confidence: float,
) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}

    def require(
        dimension: str,
        path: str,
        answer: FormAnswer,
        values: set[str],
        *,
        allow_absence: bool = False,
    ) -> None:
        value = answer.value.strip()
        quote = answer.evidence_quote
        grounded = (
            bool(quote.strip())
            and quote in body
            and answer.confidence >= min_confidence
        )
        if value in {"not_applicable", "absent", "not_mentioned"}:
            covered = (
                allow_absence
                and grounded
                and quote == body
                and bool(answer.explanation.strip())
            )
        else:
            covered = grounded and value in values
        if not covered:
            missing.setdefault(dimension, []).append(path)

    characters = {answer.name: answer for answer in answers.characters}
    for ask in form.characters:
        answer = characters[ask.name]
        if (
            ask.must_track
            or ask.prior_life_state == "dead"
            or answer.appears_in_chapter
        ):
            require(
                "life_state",
                f"character:{ask.name}.life_state",
                answer.life_state,
                {"alive", "wounded", "dead"},
                allow_absence=True,
            )
            require(
                "life_state",
                f"character:{ask.name}.participation",
                answer.participation,
                {"present_acting", "mentioned_only"},
                allow_absence=True,
            )
        if ask.prior_custody_state == "captured":
            require(
                "place",
                f"character:{ask.name}.custody_state",
                answer.custody_state,
                {"free", "captured"},
                allow_absence=True,
            )

    countdowns = {answer.key: answer for answer in answers.countdowns}
    for ask in form.countdowns:
        answer = countdowns[ask.key]
        if ask.prior_status in OPEN_COUNTDOWN_STATUSES or answer.mentioned_in_chapter:
            require(
                "time",
                f"countdown:{ask.key}.status",
                answer.status_in_this_chapter,
                {"unchanged", "advanced", "reset", "fulfilled", "reopened", "closed"},
                allow_absence=True,
            )
            require(
                "time",
                f"countdown:{ask.key}.consistency",
                answer.consistent_with_prior,
                {"true", "false"},
            )
            if answer.new_value_minutes is not None:
                if answer.new_value_evidence is None:
                    missing.setdefault("time", []).append(
                        f"countdown:{ask.key}.new_value_evidence"
                    )
                else:
                    require(
                        "time",
                        f"countdown:{ask.key}.new_value_evidence",
                        answer.new_value_evidence,
                        {str(answer.new_value_minutes)},
                    )

    obligations = {answer.id: answer for answer in answers.obligations}
    for ask in form.obligations:
        if ask.must_resolve_now:
            answer = obligations[ask.id]
            require(
                "obligations",
                f"obligation:{ask.id}.addressed",
                answer.addressed,
                {"fulfilled", "partial", "unaddressed", "explicitly_deferred"},
            )

    signals = {answer.id: answer for answer in answers.open_signals}
    for ask in form.open_signals:
        if ask.severity in BLOCKING_SIGNAL_SEVERITIES:
            # A generic critical signal has no trusted dimension classification.
            # Keep every possibly affected dimension uncovered until it is read.
            for dimension in HISTORICAL_DIMENSIONS:
                require(
                    dimension,
                    f"signal:{ask.id}.status",
                    signals[ask.id].status,
                    {"persisting", "worsened", "resolved"},
                )

    if form.final_chapter is not None and answers.final_chapter is not None:
        require(
            "obligations",
            "final_chapter.main_crisis_status",
            answers.final_chapter.main_crisis_status,
            {"closed_with_evidence", "left_dangling", "denied_or_avoided"},
        )
    return missing
