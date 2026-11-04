"""Only LARGE_TEST_DATABASE_URL is used; existing V2 integration fixtures are unrelated."""
import copy
import json
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest

from catalog_large.ingest import activate, ingest, rollback
from catalog_large.tests.test_ingest import movie, write

URL = os.getenv('LARGE_TEST_DATABASE_URL', '')
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason='LARGE_TEST_DATABASE_URL not set')]


@pytest.fixture(autouse=True)
def isolated_schema():
    info = conninfo_to_dict(URL)
    assert 'test' in info.get('dbname','').casefold()
    assert info.get('dbname') != 'ai_search'
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute('DROP SCHEMA IF EXISTS schema_v2_large CASCADE')
    yield


def q(sql, params=()):
    with psycopg.connect(URL) as c:
        return c.execute(sql, params).fetchall()


def active():
    return q('SELECT generation_id FROM schema_v2_large.active_generation')[0][0]


def test_2000_movie_import_fts_aliases_and_schema_isolation(tmp_path):
    # Sentinels prove neither original namespace is rewritten by the importer.
    with psycopg.connect(URL) as c:
        c.execute('CREATE SCHEMA IF NOT EXISTS search_v2')
        for schema in ('public','search_v2'):
            c.execute(f'CREATE TABLE IF NOT EXISTS {schema}.large_import_sentinel(value text)')
            c.execute(f'DELETE FROM {schema}.large_import_sentinel')
            c.execute(f"INSERT INTO {schema}.large_import_sentinel VALUES('preserved')")
    rows = [movie(f'M{i}') for i in range(2000)]
    for row in rows:
        row.pop('primary_language')
        row['audio_languages'] = row.pop('AudioLanguages')
    report = ingest(write(tmp_path, rows), url=URL, lexical_only=True)
    assert report['assets'] == report['documents'] == 2000
    assert report['activated'] and report['embedded'] == 0
    assert q("SELECT count(*) FROM information_schema.tables WHERE table_schema='schema_v2_large'") == [(6,)]
    assert q('SELECT count(*),count(embedding) FROM schema_v2_large.search_doc') == [(2000,0)]
    assert q("SELECT count(*) FROM schema_v2_large.search_doc WHERE tsv_main @@ plainto_tsquery('english','kidnapping child')") == [(2000,)]
    assert q("SELECT DISTINCT actor_name FROM schema_v2_large.alias_snapshot WHERE alias_norm='thalapathy'") == [('Vijay',)]
    assert q("SELECT count(*) FROM schema_v2_large.alias_snapshot WHERE entity_kind<>'actor'") == [(0,)]
    assert q("SELECT count(*) FROM schema_v2_large.asset_snapshot WHERE 'HINDI'=ANY(audio_languages) AND primary_language IS NULL") == [(2000,)]
    assert q('SELECT count(*) FROM schema_v2_large.asset_snapshot WHERE original_release_date IS NOT NULL') == [(0,)]
    for schema in ('public','search_v2'):
        assert q(f'SELECT value FROM {schema}.large_import_sentinel') == [('preserved',)]


def test_staging_activation_rollback_and_full_replacement(tmp_path):
    path = write(tmp_path, [movie('1'), movie('2')])
    first = ingest(path, url=URL, lexical_only=True)
    changed = movie('1','Salman Khan')
    changed['title'] = 'Replacement'
    path = write(tmp_path, [changed])
    second = ingest(path, url=URL, lexical_only=True, no_activate=True)
    assert active() == first['generation_id']
    assert q('SELECT asset_count FROM schema_v2_large.generation WHERE generation_id=%s', (active(),)) == [(2,)]
    with psycopg.connect(URL) as c:
        activate(c, second['generation_id'])
    assert q('SELECT title FROM schema_v2_large.asset_snapshot WHERE generation_id=%s', (active(),)) == [('Replacement',)]
    assert set(q('SELECT DISTINCT actor_name FROM schema_v2_large.alias_snapshot WHERE generation_id=%s',
                 (active(),))) == {('Salman Khan',),('Vadivelu',)}
    with psycopg.connect(URL) as c:
        rollback(c)
    assert active() == first['generation_id']
    assert q('SELECT count(*) FROM schema_v2_large.asset_snapshot WHERE generation_id=%s', (active(),)) == [(2,)]


def test_failed_embedding_build_preserves_live_snapshot(tmp_path):
    path = write(tmp_path, [movie()])
    first = ingest(path, url=URL, lexical_only=True)
    from app.embedding import HashEmbedder
    class Broken(HashEmbedder):
        def encode_docs(self, texts):
            raise RuntimeError('intentional encoder failure')
    old = os.environ.get('LARGE_ALLOW_HASH_TEST')
    os.environ['LARGE_ALLOW_HASH_TEST'] = '1'
    try:
        with pytest.raises(RuntimeError, match='intentional'):
            ingest(path, url=URL, embedder=Broken())
    finally:
        if old is None:
            os.environ.pop('LARGE_ALLOW_HASH_TEST', None)
        else:
            os.environ['LARGE_ALLOW_HASH_TEST'] = old
    assert active() == first['generation_id']
    failed = q("SELECT generation_id FROM schema_v2_large.generation WHERE status='failed'")[0][0]
    assert q('SELECT count(*) FROM schema_v2_large.asset_snapshot WHERE generation_id=%s', (failed,)) == [(0,)]
    with psycopg.connect(URL) as c, pytest.raises(ValueError, match='verified'):
        activate(c, failed)


def test_invalid_input_cannot_create_schema(tmp_path):
    broken = movie()
    broken.pop('show_id')
    with pytest.raises(ValueError):
        ingest(write(tmp_path, [broken]), url=URL, lexical_only=True)
    assert q("SELECT to_regnamespace('schema_v2_large')") == [(None,)]


def test_real_bge_vectors_reuse_and_model_safe_passages(tmp_path):
    if not os.getenv('LARGE_TEST_REAL_MODEL'):
        pytest.skip('opt in with LARGE_TEST_REAL_MODEL=1; requires installed local BGE')
    from app.embedding import load_embedder
    encoder = load_embedder('bge-small-en-v1.5')
    path = write(tmp_path, [movie('1'), movie('2')])
    first = ingest(path, url=URL, embedder=encoder)
    assert first['embedded'] == 2
    assert q('SELECT count(embedding),min(vector_dims(embedding)) FROM schema_v2_large.search_doc') == [(2,384)]
    second = ingest(path, url=URL, embedder=encoder)
    assert second['embedded'] == 0 and second['reused'] == 2
    assert second['max_tokens'] <= 384


def test_missing_primary_language_upgrades_initial_schema(tmp_path):
    from catalog_large.ingest import initialize
    with psycopg.connect(URL) as c:
        initialize(c)
        c.execute('ALTER TABLE schema_v2_large.asset_snapshot ALTER COLUMN primary_language SET NOT NULL')
    raw = movie()
    raw.pop('primary_language')
    raw['audio_languages'] = raw.pop('AudioLanguages')
    result = ingest(write(tmp_path, [raw]), url=URL, lexical_only=True)
    assert result['missing_primary_language'] == 1
    assert q('SELECT primary_language,audio_languages FROM schema_v2_large.asset_snapshot') == [(None,['TAMIL','HINDI'])]
