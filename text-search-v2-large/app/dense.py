"""Exact catalog vectors; aggregate passages per movie before the movie window."""
from dataclasses import dataclass
from app.embedding import vector_literal

@dataclass(frozen=True)
class DenseQuery:
    sql: str
    params: dict

def dense_query(*, generation, vector, window, allowed=None, exclude=None):
    sql = f'''SELECT show_id AS asset_key, max(-(embedding <#> %(vector)s::vector)) AS score
        FROM schema_v2_large.search_doc WHERE generation_id=%(gen)s AND embedding IS NOT NULL
        {"AND show_id=ANY(%(allowed)s)" if allowed is not None else ""}
        {"AND NOT(show_id=ANY(%(exclude)s))" if exclude else ""}
        GROUP BY show_id ORDER BY score DESC,show_id LIMIT %(window)s'''
    return DenseQuery(sql, dict(gen=generation,vector=vector_literal(vector),window=window,
                                allowed=allowed,exclude=exclude))

SEED_SQL = '''SELECT embedding::text AS emb FROM schema_v2_large.search_doc
    WHERE generation_id=%(gen)s AND show_id=ANY(%(keys)s) AND passage_no=1 AND embedding IS NOT NULL'''
