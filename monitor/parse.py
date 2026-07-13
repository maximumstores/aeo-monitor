# -*- coding: utf-8 -*-
"""Поиск упоминаний брендов в тексте ответа AI."""
import re

from .config import NicheConfig


def find_mentions(text: str, cfg: NicheConfig) -> list[dict]:
    """
    Для каждого бренда: mentioned, first_position (ранг появления среди брендов,
    1 = назван первым), mention_count.
    """
    low = (text or "").lower()
    first_idx: dict[str, int] = {}
    counts: dict[str, int] = {}

    for brand in cfg.all_brands:
        best = -1
        total = 0
        for alias in cfg.brand_aliases(brand):
            pattern = re.escape(alias).replace(r"\ ", r"\s+")
            hits = [m.start() for m in re.finditer(pattern, low)]
            total += len(hits)
            if hits and (best == -1 or hits[0] < best):
                best = hits[0]
        counts[brand] = total
        if best >= 0:
            first_idx[brand] = best

    order = sorted(first_idx, key=first_idx.get)  # type: ignore[arg-type]
    rank = {b: i + 1 for i, b in enumerate(order)}

    return [
        {
            "brand": b,
            "is_ours": b in cfg.ours,
            "mentioned": b in first_idx,
            "first_position": rank.get(b),
            "mention_count": counts[b],
        }
        for b in cfg.all_brands
    ]
