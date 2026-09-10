"""The existing comment analyzer's bounded directional vocabulary."""

DIRECTIONS = {
    "confusion": frozenset({"unclear", "clear", "unknown"}),
    "pacing": frozenset({"too_slow", "too_fast", "balanced", "unknown"}),
    "character_heat": frozenset({"positive", "negative", "unknown"}),
    "risk": frozenset({"concern", "unknown"}),
    "relationship_interest": frozenset({"want_more", "want_less", "unknown"}),
    "prediction": frozenset({"predicts", "unknown"}),
}


def validated_direction(signal_type, direction, evidence, body):
    value = str(direction or "unknown").strip().lower()
    if value not in DIRECTIONS.get(signal_type, ()):
        raise ValueError("comment direction is not valid for its signal type")
    # A model label with no grounded quote is only an observation, never a direction.
    return value if evidence and evidence in body else "unknown"


def keyword_direction(signal_type, body):
    if signal_type == "pacing":
        slow = any(word in body for word in ("太慢", "太拖", "拖沓", "太水"))
        fast = any(word in body for word in ("太快", "太赶", "赶进度"))
        return (
            "too_slow"
            if slow and not fast
            else "too_fast"
            if fast and not slow
            else "unknown"
        )
    if signal_type == "character_heat":
        positive = any(word in body for word in ("喜欢", "精彩", "好看", "期待"))
        negative = any(word in body for word in ("讨厌", "反感", "恶心"))
        return (
            "positive"
            if positive and not negative
            else "negative"
            if negative and not positive
            else "unknown"
        )
    if signal_type == "relationship_interest":
        more = any(word in body for word in ("多点互动", "多些互动", "想看", "在一起"))
        less = any(word in body for word in ("少点", "别再", "不想看"))
        return (
            "want_more"
            if more and not less
            else "want_less"
            if less and not more
            else "unknown"
        )
    if signal_type == "confusion":
        return (
            "unclear"
            if any(word in body for word in ("看不懂", "不理解", "为什么"))
            else "unknown"
        )
    if signal_type == "risk":
        return "concern"
    if signal_type == "prediction":
        return "predicts"
    return "unknown"
