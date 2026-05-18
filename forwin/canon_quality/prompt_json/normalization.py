from __future__ import annotations

from typing import Any

from forwin.canon_quality.signals import CanonQualitySignal, make_signal_id

from .validation import issue_can_block, normalize_result


def legacy_signal_to_hint(signal: CanonQualitySignal | dict[str, Any]) -> dict[str, Any]:
    item = signal.model_dump(mode="json") if isinstance(signal, CanonQualitySignal) else dict(signal)
    return {
        "hint_type": str(item.get("signal_type") or "legacy_signal"),
        "message": str(item.get("description") or ""),
        "matched_text": "",
        "legacy_signal_id": str(item.get("signal_id") or ""),
        "legacy_severity": str(item.get("severity") or ""),
        "evidence_refs": list(item.get("evidence_refs") or []),
        "payload": item.get("payload") if isinstance(item.get("payload"), dict) else {},
    }


def legacy_signals_to_hints(signals: list[CanonQualitySignal | dict[str, Any]]) -> list[dict[str, Any]]:
    return [legacy_signal_to_hint(signal) for signal in signals]


def legacy_signals_to_prompt_result(
    *,
    analyzer: str,
    legacy_signals: list[CanonQualitySignal | dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    hints = legacy_signals_to_hints(legacy_signals)
    issues: list[dict[str, Any]] = []
    for index, hint in enumerate(hints, start=1):
        legacy_severity = str(hint.get("legacy_severity") or "")
        severity = "major" if legacy_severity == "error" else "minor"
        issues.append(
            {
                "issue_id": str(hint.get("legacy_signal_id") or f"legacy_hint:{index}"),
                "type": str(hint.get("hint_type") or "legacy_hint"),
                "severity": severity,
                "blocking": False,
                "confidence": 0.0,
                "claim": str(hint.get("message") or ""),
                "evidence": [],
                "reasoning_summary": "Legacy deterministic signal retained as a hint only.",
                "suggested_fix": "",
                "legacy_hint": hint,
            }
        )
    return normalize_result(
        {
            "analyzer": analyzer,
            "version": "1.0",
            "verdict": "uncertain" if hints else "pass",
            "blocking": False,
            "confidence": 0.0,
            "summary": (
                f"{analyzer} did not make a prompt-json blocking decision; "
                f"{len(hints)} legacy hints retained."
            ),
            "issues": issues,
            "accepted_facts": [],
            "uncertainties": [
                {
                    "question": "Prompt analyzer unavailable",
                    "why_uncertain": "Only legacy heuristic hints were available.",
                    "needed_context": "Run prompt-json analyzer with an LLM client.",
                }
            ] if hints else [],
            "metadata": {
                "source_mode": mode,
                "legacy_signal_count": len(hints),
                "legacy_hints": hints,
            },
        },
        analyzer=analyzer,
    )


def prompt_issue_evidence_refs(issue: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for evidence in issue.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        source = str(evidence.get("source") or "prompt_json").strip()
        location = str(evidence.get("location") or "").strip()
        quote = str(evidence.get("quote") or "").strip()
        if location:
            refs.append(f"{source}:{location}")
        elif quote:
            refs.append(f"{source}:{quote[:40]}")
    return refs


def prompt_results_to_signals(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    results: list[dict[str, Any]],
    min_blocking_confidence: float = 0.8,
) -> list[CanonQualitySignal]:
    signals: list[CanonQualitySignal] = []
    for result in results:
        analyzer = str(result.get("analyzer") or "PromptJsonAnalyzer")
        for index, issue in enumerate(result.get("issues") or [], start=1):
            if not isinstance(issue, dict):
                continue
            can_block = issue_can_block(issue, min_confidence=min_blocking_confidence)
            severity = "error" if can_block else "warning"
            signal_type = str(issue.get("type") or "prompt_json_issue")
            subject = f"{analyzer}:{signal_type}:{index}"
            evidence_refs = prompt_issue_evidence_refs(issue)
            signals.append(
                CanonQualitySignal(
                    signal_id=make_signal_id(project_id, chapter_number, f"prompt_json_{signal_type}", subject, index),
                    project_id=project_id,
                    chapter_number=int(chapter_number or 0),
                    signal_type=signal_type,
                    severity=severity,
                    target_scope="chapter",
                    subject_key=subject,
                    description=str(issue.get("claim") or result.get("summary") or signal_type),
                    evidence_refs=evidence_refs,
                    payload={
                        "draft_id": draft_id,
                        "source_layer": "canon_quality",
                        "source_analyzer": analyzer,
                        "source_mode": "prompt_json",
                        "original_verdict": str(result.get("verdict") or ""),
                        "original_confidence": float(result.get("confidence") or 0.0),
                        "blocking_origin": "prompt_json" if can_block else "non_blocking_prompt_json",
                        "prompt_json_issue": issue,
                        "prompt_json_metadata": result.get("metadata") if isinstance(result.get("metadata"), dict) else {},
                    },
                )
            )
    return signals


def normalize_prompt_json_results_to_review_issues(
    analysis_results: list[dict[str, Any]],
    *,
    source_layer: str,
) -> list[dict[str, Any]]:
    review_issues: list[dict[str, Any]] = []
    for result in analysis_results:
        analyzer = str(result.get("analyzer") or "PromptJsonAnalyzer")
        verdict = str(result.get("verdict") or "uncertain")
        confidence = float(result.get("confidence") or 0.0)
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        source_mode = str(metadata.get("source_mode") or "prompt_json")
        for issue in result.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            if str(issue.get("severity") or "") == "info":
                continue
            review_issues.append(
                {
                    "issue_id": str(issue.get("issue_id") or ""),
                    "reviewer_issue_type": str(issue.get("type") or "canon_quality"),
                    "source_layer": source_layer,
                    "source_analyzer": analyzer,
                    "source_mode": source_mode,
                    "severity": str(issue.get("severity") or "minor"),
                    "blocking": bool(issue.get("blocking", False)),
                    "confidence": float(issue.get("confidence") or 0.0),
                    "claim": str(issue.get("claim") or ""),
                    "evidence": list(issue.get("evidence") or []),
                    "evidence_refs": prompt_issue_evidence_refs(issue),
                    "suggested_fix": str(issue.get("suggested_fix") or ""),
                    "original_result": result,
                    "original_verdict": verdict,
                    "original_confidence": confidence,
                    "blocking_origin": "prompt_json" if issue_can_block(issue) else "non_blocking_prompt_json",
                }
            )
    return review_issues
