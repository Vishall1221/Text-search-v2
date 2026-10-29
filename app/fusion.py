"""Weighted Reciprocal Rank Fusion and scene -> asset aggregation.

* Every channel is a ranked list of (asset_key, scene_id|None). Raw scores are never mixed across
  channels: only ranks are, which is why BM25-like and cosine scores need no calibration to coexist.
* Scene channels vote for an asset once (MaxP: its best-ranked scene defines the asset's rank), so an
  asset with many similar scenes cannot crowd the list. The optional multi-scene bonus (lambda) is 0 by
  default and exists so the benchmark can decide whether it is worth turning on.
* The same function fuses scenes inside an asset (scene-level RRF over the scene channels).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Hit:
    asset_key: str
    scene_id: str | None
    score: float


@dataclass
class SceneResult:
    scene_id: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)


@dataclass
class AssetResult:
    asset_key: str
    score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)          # channel -> asset rank (1-based)
    contributions: dict[str, float] = field(default_factory=dict)
    scenes: list[SceneResult] = field(default_factory=list)


def dedupe(hits: list[Hit], by_scene: bool) -> list[Hit]:
    seen: set = set()
    out = []
    for h in hits:
        key = (h.asset_key, h.scene_id) if by_scene else h.asset_key
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def fuse(channels: dict[str, list[Hit]], weights: dict[str, float], scene_channels: set[str], *,
         k: int = 60, window: int = 50, bonus: float = 0.0, bonus_scenes: int = 3) -> list[AssetResult]:
    assets: dict[str, AssetResult] = {}
    scene_rank_lists: dict[str, dict[str, list[tuple[str, int]]]] = defaultdict(lambda: defaultdict(list))
    for name, hits in channels.items():
        w = weights.get(name, 1.0)
        if w <= 0 or not hits:
            continue
        # asset-level ranking inside the channel (one vote per asset)
        distinct = dedupe(hits, by_scene=False)[:window]
        for rank, h in enumerate(distinct, start=1):
            a = assets.setdefault(h.asset_key, AssetResult(h.asset_key))
            a.ranks[name] = rank
            c = w / (k + rank)
            a.score += c
            a.contributions[name] = c
        if name in scene_channels:
            per_scene = dedupe(hits, by_scene=True)
            allowed = {h.asset_key for h in distinct}
            for rank, h in enumerate(per_scene, start=1):
                if h.asset_key in allowed and h.scene_id is not None:
                    scene_rank_lists[h.asset_key][name].append((h.scene_id, rank))
    for key, per_channel in scene_rank_lists.items():
        scenes: dict[str, SceneResult] = {}
        for name, lst in per_channel.items():
            w = weights.get(name, 1.0)
            for sid, rank in lst:
                s = scenes.setdefault(sid, SceneResult(sid, 0.0))
                s.score += w / (k + rank)
                s.ranks[name] = rank
        ordered = sorted(scenes.values(), key=lambda s: (-s.score, s.scene_id))
        assets[key].scenes = ordered
        if bonus > 0 and len(ordered) > 1:
            # extra credit for further scenes of the same asset in the best-voting scene channel
            best = max(per_channel.items(), key=lambda kv: weights.get(kv[0], 1.0))
            extra = sorted(r for _, r in best[1])[1:bonus_scenes]
            gain = bonus * sum(weights.get(best[0], 1.0) / (k + r) for r in extra)
            assets[key].score += gain
            assets[key].contributions["multi_scene_bonus"] = gain
    return sorted(assets.values(), key=lambda a: (-a.score, a.asset_key))
