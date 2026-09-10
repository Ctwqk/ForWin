"""Parse explicit durations, in hours; unsupported prose stays unknown."""
from __future__ import annotations

import re

_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)个?半?|半"
_PART = re.compile(rf"(?P<number>{_NUMBER})\s*(?P<unit>分钟|小时|时辰|秒钟|秒|天|日)")
_FACTORS = {"分钟": 1 / 60, "小时": 1, "时辰": 2, "秒钟": 1 / 3600, "秒": 1 / 3600, "天": 24, "日": 24}


def duration_hours(text: str) -> float | None:
    value = str(text or "").strip()
    value = re.sub(r"^.*(?:历时|耗时|共计|持续)", "", value)
    value = value.strip(" 。.,，")
    value = re.sub(r"(?:之后|以后|后)$", "", value)
    value = value.replace("半天", "半日").replace("半个时辰", "半时辰")
    parts = list(_PART.finditer(value))
    if not parts or re.sub(r"[\s又零]", "", _PART.sub("", value)):
        return None
    total = 0.0
    for part in parts:
        raw = part["number"].replace("个", "")
        half = 0.5 if raw.endswith("半") else 0.0
        raw = raw.removesuffix("半")
        if not raw:
            number = 0.0
        elif re.fullmatch(r"\d+(?:\.\d+)?", raw):
            number = float(raw)
        else:
            digits = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
            number, digit = 0.0, 0
            for character in raw:
                if character in digits:
                    digit = digits[character]
                else:
                    number += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[character]
                    digit = 0
            number += digit
        total += (number + half) * _FACTORS[part["unit"]]
    return total
