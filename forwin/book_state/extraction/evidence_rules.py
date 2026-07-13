from __future__ import annotations

from dataclasses import dataclass

from forwin.protocol.world_v4 import DeltaSourceType


@dataclass(frozen=True)
class BodyEvidenceSpan:
    label: str
    start: int
    end: int
    text: str

    @property
    def source_ref(self) -> str:
        return f"body_span:{self.label}:{self.start}:{self.end}"


def _find_keyword_spans(
    body: str,
    label: str,
    keywords: tuple[str, ...],
) -> list[BodyEvidenceSpan]:
    spans: list[BodyEvidenceSpan] = []
    for keyword in keywords:
        start = str(body or "").find(keyword)
        if start < 0:
            continue
        end = start + len(keyword)
        spans.append(BodyEvidenceSpan(label=label, start=start, end=end, text=keyword))
    return spans


def find_hint_spans(body: str) -> list[BodyEvidenceSpan]:
    return _find_keyword_spans(
        body,
        "hint",
        ("乱码", "旧部呼号", "呼号", "通讯延迟", "残缺求援"),
    )


def find_offscreen_spans(body: str) -> list[BodyEvidenceSpan]:
    return _find_keyword_spans(
        body,
        "offscreen",
        ("敌方切断", "切断第三通讯阵列", "敌军开始围困", "舰队围困"),
    )


def infer_source_type(text: str) -> DeltaSourceType:
    if any(token in text for token in ("敌方", "敌军", "舰队", "切断")):
        return DeltaSourceType.FACTION_ACTION
    if any(token in text for token in ("通讯", "呼号", "求援", "乱码")):
        return DeltaSourceType.INFORMATION_SPREAD
    return DeltaSourceType.CHARACTER_ACTION
