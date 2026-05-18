from __future__ import annotations

from typing import Any

from .schemas import VALID_SEVERITIES, VALID_VERDICTS


class PromptJsonValidationError(ValueError):
    pass


def clamp_confidence(value: object, *, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    return max(0.0, min(1.0, numeric))


def normalize_evidence(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    evidence: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        location = str(item.get("location") or "").strip()
        source = str(item.get("source") or "").strip() or "derived"
        if not quote and not location:
            continue
        evidence.append(
            {
                "source": source,
                "quote": quote,
                "location": location,
            }
        )
    return evidence


def normalize_issue(raw: object, *, index: int = 1) -> dict[str, Any]:
    item = dict(raw) if isinstance(raw, dict) else {}
    severity = str(item.get("severity") or "minor").strip().lower()
    if severity not in VALID_SEVERITIES:
        severity = "minor"
    issue_type = str(item.get("type") or "prompt_json_issue").strip() or "prompt_json_issue"
    issue_id = str(item.get("issue_id") or f"{issue_type}:{index}").strip()
    return {
        **item,
        "issue_id": issue_id,
        "type": issue_type,
        "severity": severity,
        "blocking": bool(item.get("blocking", False)),
        "confidence": clamp_confidence(item.get("confidence"), default=0.0),
        "claim": str(item.get("claim") or "").strip(),
        "evidence": normalize_evidence(item.get("evidence")),
        "reasoning_summary": str(item.get("reasoning_summary") or "").strip(),
        "suggested_fix": str(item.get("suggested_fix") or "").strip(),
    }


def normalize_result(
    raw: object,
    *,
    analyzer: str,
    version: str = "1.0",
    prompt_version: str = "1.0",
    input_hash: str = "",
    model: str = "",
    min_blocking_confidence: float = 0.8,
) -> dict[str, Any]:
    result = dict(raw) if isinstance(raw, dict) else {}
    verdict = str(result.get("verdict") or "uncertain").strip().lower()
    if verdict not in VALID_VERDICTS:
        verdict = "uncertain"
    issues = [
        normalize_issue(issue, index=index)
        for index, issue in enumerate(result.get("issues") if isinstance(result.get("issues"), list) else [], start=1)
    ]
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    normalized = {
        **result,
        "analyzer": str(result.get("analyzer") or analyzer),
        "version": str(result.get("version") or version),
        "verdict": verdict,
        "confidence": clamp_confidence(result.get("confidence"), default=0.0),
        "summary": str(result.get("summary") or "").strip(),
        "issues": issues,
        "accepted_facts": result.get("accepted_facts") if isinstance(result.get("accepted_facts"), list) else [],
        "uncertainties": result.get("uncertainties") if isinstance(result.get("uncertainties"), list) else [],
        "metadata": {
            **metadata,
            "input_hash": str(metadata.get("input_hash") or input_hash),
            "model": str(metadata.get("model") or model),
            "prompt_version": str(metadata.get("prompt_version") or prompt_version),
        },
    }
    normalized["blocking"] = result_can_block(
        normalized,
        min_confidence=min_blocking_confidence,
    )
    return normalized


def issue_can_block(
    issue: dict[str, Any],
    min_confidence: float = 0.8,
    *,
    require_evidence: bool = True,
) -> bool:
    if str(issue.get("severity") or "").strip().lower() != "critical":
        return False
    if issue.get("blocking") is not True:
        return False
    if clamp_confidence(issue.get("confidence"), default=0.0) < float(min_confidence):
        return False
    if require_evidence and not normalize_evidence(issue.get("evidence")):
        return False
    return True


def result_can_block(
    result: dict[str, Any],
    min_confidence: float = 0.8,
    *,
    require_evidence: bool = True,
) -> bool:
    if str(result.get("verdict") or "").strip().lower() in {"pass", "warn", "uncertain"}:
        return False
    issues = result.get("issues") if isinstance(result.get("issues"), list) else []
    return any(
        issue_can_block(
            issue if isinstance(issue, dict) else {},
            min_confidence=min_confidence,
            require_evidence=require_evidence,
        )
        for issue in issues
    )


def validate_json_schema(result: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    missing = [
        field
        for field in schema.get("required", [])
        if field not in result
    ]
    if missing:
        raise PromptJsonValidationError(f"prompt JSON result missing fields: {', '.join(missing)}")
    return result


def repair_or_reask_if_invalid(result: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    analyzer = str(schema.get("analyzer") or result.get("analyzer") or "PromptJsonAnalyzer")
    return normalize_result(result, analyzer=analyzer)
