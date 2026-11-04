"""Read-only snapshot loader and async serving pool for schema_v2_large only."""
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from app.gazetteer import Catalog, asset_from_row
from config.settings import settings


def make_pool():
    return AsyncConnectionPool(settings.database_url, min_size=settings.db_pool_min,
        max_size=settings.db_pool_max, open=False, timeout=2,
        kwargs={'row_factory': dict_row, 'autocommit': True}, name='catalog_large')


def load_catalog(conn):
    gen = conn.execute('''SELECT g.* FROM schema_v2_large.generation g
        JOIN schema_v2_large.active_generation a USING(generation_id)''').fetchone()
    if not gen:
        raise RuntimeError('No active large catalog. Import with python -m catalog_large.ingest first.')
    gid = gen['generation_id']
    rows = list(conn.execute('SELECT * FROM schema_v2_large.asset_snapshot WHERE generation_id=%s ORDER BY show_id', (gid,)))
    aliases = list(conn.execute('SELECT * FROM schema_v2_large.alias_snapshot WHERE generation_id=%s', (gid,)))
    lexicon = {r['word']: r['df'] for r in conn.execute('SELECT word,df FROM schema_v2_large.lexicon WHERE generation_id=%s', (gid,))}
    if len(rows) != gen['asset_count']:
        raise RuntimeError('Catalog snapshot membership is inconsistent')
    cat = Catalog([asset_from_row(r) for r in rows], actor_aliases=aliases, lexicon=lexicon,
                  generation=gen, doc_total=gen['doc_count'])
    cat.metadata = {r['show_id']: r for r in rows}
    return cat


def load_catalog_sync(url=None):
    with psycopg.connect(url or settings.database_url, row_factory=dict_row, connect_timeout=3) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout='2000ms'")
        return load_catalog(conn)
