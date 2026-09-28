"""Comment-tree helpers: ancestors / descendants and the "ancestors + replies" pruning."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable


def index_tree(items: Iterable[dict]) -> tuple[dict[int, dict], dict[int, list[int]]]:
    by_id: dict[int, dict] = {}
    children: dict[int, list[int]] = defaultdict(list)
    for c in items:
        by_id[c["id"]] = c
    for c in by_id.values():
        p = c.get("replyTo") or 0
        if p:
            children[p].append(c["id"])
    for lst in children.values():
        lst.sort(key=lambda i: (by_id[i].get("date", 0), i))
    return by_id, children


def ancestors(cid: int, by_id: dict[int, dict]) -> list[int]:
    """Parent chain from the root down to the direct parent (excluding cid)."""
    chain: list[int] = []
    seen = {cid}
    p = by_id.get(cid, {}).get("replyTo") or 0
    while p and p not in seen:
        seen.add(p)
        chain.append(p)
        p = by_id.get(p, {}).get("replyTo") or 0
    chain.reverse()
    return chain


def descendants(cid: int, children: dict[int, list[int]]) -> list[int]:
    out: list[int] = []
    stack = list(reversed(children.get(cid, [])))
    seen = {cid}
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
        stack.extend(reversed(children.get(x, [])))
    return out


def prune_context(items: list[dict], my_ids: Iterable[int]) -> tuple[list[dict], list[int]]:
    """Keep, for every comment in my_ids: its ancestor chain, itself and its whole reply subtree.
    Returns (kept items in original order, my ids not present in items)."""
    by_id, children = index_tree(items)
    keep: set[int] = set()
    missing: list[int] = []
    for mid in my_ids:
        if mid not in by_id:
            missing.append(mid)
            continue
        keep.add(mid)
        keep.update(a for a in ancestors(mid, by_id) if a in by_id)
        keep.update(descendants(mid, children))
    return [c for c in items if c["id"] in keep], missing
