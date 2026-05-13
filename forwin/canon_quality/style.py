from __future__ import annotations

from collections import Counter
from typing import Any

from .signals import CanonQualitySignal, StyleTelemetry, make_signal_id

STYLE_MOTIFS = ("铁锈味", "旧纸味", "冷白光", "通风管道", "密钥发热", "脚步声", "警报")
DIALOGUE_TEMPLATES = ("你疯了", "别回头", "你知道这意味着什么吗")


def analyze_style_telemetry(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
    previous_metrics: list[dict[str, Any] | StyleTelemetry] | None = None,
) -> tuple[list[CanonQualitySignal], StyleTelemetry]:
    text = str(body or "")
    current_motifs = [motif for motif in (*STYLE_MOTIFS, *DIALOGUE_TEMPLATES) if motif in text]
    counter: Counter[str] = Counter(current_motifs)
    for raw in previous_metrics or []:
        item = raw.model_dump(mode="json") if isinstance(raw, StyleTelemetry) else dict(raw)
        counter.update(str(motif) for motif in item.get("style_motifs", []) if str(motif).strip())
    top_repeated = [motif for motif, count in counter.most_common() if count >= 2]
    telemetry = StyleTelemetry(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        style_motifs=current_motifs,
        top_repeated_motifs=top_repeated[:8],
        dialogue_templates=[template for template in DIALOGUE_TEMPLATES if template in text],
        rolling_window_density=(sum(counter.values()) / max(1, len(previous_metrics or []) + 1)),
        metrics={"motif_counts": dict(counter)},
    )
    signals: list[CanonQualitySignal] = []
    if top_repeated:
        subject = f"style:{top_repeated[0]}"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "style_repetition", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="style_repetition",
                severity="warning",
                target_scope="chapter",
                subject_key=subject,
                description=f"近章高频重复风格模板：{', '.join(top_repeated[:5])}。",
                evidence_refs=[f"style:{motif}" for motif in top_repeated[:5]],
                payload={"draft_id": draft_id, "telemetry": telemetry.model_dump(mode="json")},
            )
        )
    return signals, telemetry
