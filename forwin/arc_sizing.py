from __future__ import annotations

from dataclasses import dataclass


def _clamp_int(value: float | int, lower: int, upper: int) -> int:
    return max(lower, min(int(round(value)), upper))


@dataclass(slots=True)
class ArcPolicyTier:
    name: str
    ratio: float
    min_size: int
    max_size: int
    soft_min_ratio: float
    soft_max_ratio: float


_ARC_POLICY_TIERS = [
    ((1, 150), ArcPolicyTier("short", 0.18, 12, 24, 0.75, 1.25)),
    ((151, 400), ArcPolicyTier("medium", 0.15, 16, 30, 0.65, 1.50)),
    ((401, 800), ArcPolicyTier("long", 0.10, 20, 40, 0.55, 1.70)),
    ((801, 10**9), ArcPolicyTier("ultra-long", 0.08, 24, 48, 0.50, 2.00)),
]


def policy_for_total_chapters(total_chapters: int) -> ArcPolicyTier:
    total = max(1, int(total_chapters or 1))
    for (lower, upper), policy in _ARC_POLICY_TIERS:
        if lower <= total <= upper:
            return policy
    return _ARC_POLICY_TIERS[0][1]


def recommended_arc_target_size(total_chapters: int) -> int:
    total = max(1, int(total_chapters or 1))
    policy = policy_for_total_chapters(total)
    return _clamp_int(total * policy.ratio, policy.min_size, policy.max_size)


def allocate_arc_chapter_sizes(total_chapters: int) -> list[int]:
    total = max(1, int(total_chapters or 1))
    if total <= 12:
        return [total]

    policy = policy_for_total_chapters(total)
    target = recommended_arc_target_size(total)
    soft_min = max(4, int(round(target * policy.soft_min_ratio)))
    sizes: list[int] = []
    remaining = total

    while remaining > 0:
        if remaining <= target:
            if sizes and remaining < soft_min:
                sizes[-1] += remaining
            else:
                sizes.append(remaining)
            break

        next_size = target
        tail = remaining - next_size
        if 0 < tail < soft_min:
            next_size = max(soft_min, remaining - soft_min)

        sizes.append(next_size)
        remaining -= next_size

    return sizes
