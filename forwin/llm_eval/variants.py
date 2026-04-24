from __future__ import annotations

import hashlib


def variant_seed(run_id: str, case_id: str, profile_id: str, iteration: int) -> str:
    raw = f"{run_id}:{case_id}:{profile_id}:{int(iteration)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def apply_cache_buster(
    messages: list[dict],
    *,
    run_id: str,
    variant_seed: str,
) -> list[dict]:
    marker = (
        f"\n\n测试批次代号：{run_id}:{variant_seed[:12]}。"
        "该代号只用于避免缓存命中，禁止在输出中提及。"
    )
    updated: list[dict] = []
    injected = False
    for item in messages:
        cloned = dict(item)
        role = str(cloned.get("role", "")).strip()
        if not injected and role in {"user", "system"}:
            cloned["content"] = str(cloned.get("content", "")) + marker
            injected = True
        updated.append(cloned)
    if not injected:
        updated.append({"role": "user", "content": marker.strip()})
    return updated
