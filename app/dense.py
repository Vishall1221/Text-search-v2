"""Dense retrieval: exact inner-product search over normalised embeddings (pgvector, no ANN index).

``<#>`` returns the NEGATIVE inner product; for unit vectors similarity = -(a <#> b). An approximate
index (HNSW) is added only if profiling shows dense p95 above the budget (see README).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.embedding import vector_literal
from config.settings import settings
import numpy as np


@dataclass(frozen=True)
class DenseQuery:
    sql: str
    params: dict


def dense_query(*, generation: int, scope: str, kinds: tuple[str, ...], vector: np.ndarray, window: int,
                allowed: list[str] | None, exclude: list[str] | None = None) -> DenseQuery:
    # Stage 1 is a plain nearest-neighbour top-k: the only query shape an ANN (HNSW) index can serve, and
    # a cheap top-N sort today. Stage 2 groups that candidate pool by scene and title, so one long film
    # cannot crowd other titles out of the window. Grouping ALL documents instead (the previous form)
    # was ~3x slower at 41k documents (110 vs 38 ms) and can never use an index.
    sql = f"""
        WITH candidates AS MATERIALIZED (
        SELECT d.asset_key, d.scene_id, -(d.embedding <#> %(qv)s::vector) AS score
        FROM search_v2.search_doc d
        WHERE d.generation_id = %(gen)s AND d.scope = %(scope)s AND d.kind = ANY(%(kinds)s)
          {"AND d.asset_key = ANY(%(allowed)s)" if allowed is not None else ""}
          {"AND NOT (d.asset_key = ANY(%(exclude)s))" if exclude else ""}
        ORDER BY d.embedding <#> %(qv)s::vector
        LIMIT %(candidates)s
        ), scored AS MATERIALIZED (
            SELECT asset_key, scene_id, max(score) AS score FROM candidates GROUP BY asset_key, scene_id
        ), selected_assets AS (
            SELECT asset_key, max(score) AS best_score FROM scored GROUP BY asset_key
            ORDER BY best_score DESC, asset_key LIMIT %(asset_window)s
        ), ranked AS (
            SELECT s.*, row_number() OVER (PARTITION BY s.asset_key ORDER BY s.score DESC, s.scene_id) AS scene_rank
            FROM scored s JOIN selected_assets a USING (asset_key)
        )
        SELECT asset_key, scene_id, score FROM ranked WHERE scene_rank <= %(scene_limit)s
        ORDER BY score DESC, asset_key, scene_id"""
    params: dict = {"qv": vector_literal(vector), "gen": generation, "scope": scope, "kinds": list(kinds),
                    "k": window * 3, "asset_window": window, "candidates": max(settings.dense_candidates, window),
                    "scene_limit": max(settings.scenes_per_card * 2, 1) if scope == "scene" else 1}
    if allowed is not None:
        params["allowed"] = allowed
    if exclude:
        params["exclude"] = exclude
    return DenseQuery(sql, params)


SEED_SQL = """SELECT asset_key, embedding::text AS emb FROM search_v2.search_doc
              WHERE generation_id = %(gen)s AND scope = 'catalog' AND kind = 'main' AND passage_no = 1
                AND asset_key = ANY(%(keys)s)"""
