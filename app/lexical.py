"""Lexical retrieval on native PostgreSQL full-text search (no BM25 extension required).

* ``tsv_main``   : weighted 'english' vectors (stemmed). Ranked with ts_rank_cd (cover density), length
                   normalisation flag from settings. ts_rank_cd is NOT BM25: it has no IDF, so very common
                   words matter as much as rare ones. The planner therefore orders terms by rarity and the
                   relaxation stages below decide how many must match.
* ``tsv_simple`` : 'simple' vectors for dialogue (no stop-word removal, no stemming), so a quoted phrase such
                   as "I will return" keeps every word.

Relaxation stages for a multi-term query, strictest first; the first stage that returns at least
``lex_min_candidates`` documents wins:

    and       every term
    but_one   every term except one (any one)           (3+ terms)
    relaxed   at least ``min_match(n)`` of the n terms  (2 terms: either one)

The minimum-match rule is written INTO the tsquery as an OR of AND-combinations, not applied afterwards. That is
what stops a single common word from "matching" an out-of-catalog request, and it keeps the candidate set small:
a plain OR of six terms matched ~2,900 of 20,000 scene documents and every one had to be scored by ts_rank_cd
(~29 ms), while "3 of the 6 terms" matched 1 document in under 1 ms (measured on the 20,500-scene scale test; see README).
Every statement runs under a statement_timeout set by the caller.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import combinations

from config.settings import settings

_WORD = re.compile(r"[^\w]", re.UNICODE)
MAX_TERMS = 8
RANK_FUNCS = {"ts_rank_cd": "ts_rank_cd", "ts_rank": "ts_rank"}


def clean_terms(terms: list[str]) -> list[str]:
    out = []
    for t in terms:
        c = _WORD.sub("", t)
        if c and c not in out:
            out.append(c)
    return out[:MAX_TERMS]


def min_match(n_terms: int) -> int:
    """Terms a document must contain in the relaxed stages (<= 2 terms: any one)."""
    if n_terms <= 2:
        return 1
    return max(1, math.ceil(settings.lex_min_match_ratio * n_terms))


def _any_of(terms: list[str], size: int) -> str:
    """tsquery text: documents containing at least ``size`` of ``terms`` (OR of all AND-combinations)."""
    return " | ".join("(" + " & ".join(c) + ")" for c in combinations(terms, size))


def stages(terms: list[str], relax: bool) -> list[tuple[str, str]]:
    """[(stage name, tsquery text)] from strictest to loosest."""
    terms = clean_terms(terms)
    if not terms:
        return []
    n = len(terms)
    out = [("and", " & ".join(terms))]
    if not relax or n < 2:
        return out
    need = min(min_match(n), n)
    if need >= n:                                   # a ratio of 1.0 means "all words": nothing to relax
        return out
    if n >= 3:
        out.append(("but_one", _any_of(terms, n - 1)))
    if need < n - 1 or n == 2:                      # (for n == need + 1 the but_one stage is already this stage)
        out.append(("relaxed", _any_of(terms, need)))
    return out


@dataclass(frozen=True)
class LexQuery:
    sql: str
    params: dict


def _rank_fn() -> str:
    return RANK_FUNCS.get(settings.lex_rank_fn, "ts_rank_cd")


def _build(*, tsv: str, query_expr: str, scope_sql: str, generation: int, window: int,
           allowed: list[str] | None, query: str, extra: dict) -> LexQuery:
    sql = f"""
        WITH q AS (SELECT {query_expr} AS tsq), scored AS MATERIALIZED (
        SELECT d.asset_key, d.scene_id, max({_rank_fn()}(d.{tsv}, q.tsq, %(norm)s)) AS score
        FROM search_v2.search_doc d, q
        WHERE d.generation_id = %(gen)s AND {scope_sql} AND d.{tsv} @@ q.tsq
          {"AND d.asset_key = ANY(%(allowed)s)" if allowed is not None else ""}
        GROUP BY d.asset_key, d.scene_id
        ), selected_assets AS (
            SELECT asset_key, max(score) AS best_score FROM scored GROUP BY asset_key
            ORDER BY best_score DESC, asset_key LIMIT %(asset_window)s
        ), ranked AS (
            SELECT s.*, row_number() OVER (PARTITION BY s.asset_key ORDER BY s.score DESC, s.scene_id) AS scene_rank
            FROM scored s JOIN selected_assets a USING (asset_key)
        )
        SELECT asset_key, scene_id, score FROM ranked WHERE scene_rank <= %(scene_limit)s
        ORDER BY score DESC, asset_key, scene_id"""
    params = {"q": query, "gen": generation, "norm": settings.lex_rank_norm, "k": window * 3,
              "asset_window": window, "scene_limit": max(settings.scenes_per_card * 2, 1), **extra}
    if allowed is not None:
        params["allowed"] = allowed
    return LexQuery(sql, params)


def main_query(*, generation: int, scope: str, tsquery: str, window: int, allowed: list[str] | None) -> LexQuery:
    """Scene or catalog 'main' documents (english configuration)."""
    return _build(tsv="tsv_main", query_expr="to_tsquery('english', %(q)s)",
                  scope_sql="d.scope = %(scope)s AND d.kind = 'main'", generation=generation, window=window,
                  allowed=allowed, query=tsquery, extra={"scope": scope})


def dialogue_query(*, generation: int, tsquery: str, window: int, allowed: list[str] | None,
                   phrase: bool = False, plain: bool = False) -> LexQuery:
    """Dialogue documents ('simple' configuration). ``phrase``: phraseto_tsquery on raw text (adjacent words);
    ``plain``: plainto_tsquery (all words, any order); otherwise a prepared to_tsquery string."""
    expr = ("phraseto_tsquery('simple', %(q)s)" if phrase else
            "plainto_tsquery('simple', %(q)s)" if plain else "to_tsquery('simple', %(q)s)")
    return _build(tsv="tsv_simple", query_expr=expr,
                  scope_sql="d.scope = 'scene' AND d.kind = 'dialogue'", generation=generation, window=window,
                  allowed=allowed, query=tsquery, extra={})
