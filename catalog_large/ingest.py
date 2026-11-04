"""python -m catalog_large.ingest --data Movie_metadata.json [--dry-run|--lexical-only]."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from app.documents import split_passages
from app.embedding import approx_tokens, load_embedder, vector_literal
from app.text import fold
from config.settings import settings
from catalog_large.source import DEFAULT_ALIASES, build_aliases, read_movies

SCHEMA_FILE = Path(__file__).with_name('schema.sql')
TEXT_VERSION = 'large-catalog-1'
log = logging.getLogger(__name__)


def database_url():
    # Never log the returned connection string.
    return os.getenv('LARGE_DATABASE_URL') or settings.database_url


def passages(movies, embedder=None, tokens=384):
    count = embedder.count_tokens if embedder else approx_tokens
    limit = min(tokens, embedder.profile.max_len, 512) if embedder else tokens
    if limit < 16:
        raise ValueError('Passage token budget must be at least 16')
    identity = (f'{embedder.profile.name}:{embedder.revision}:{embedder.profile.pooling}' if embedder else 'lexical')
    out = []
    for movie in movies:
        units = [movie['title']+'.']
        if movie['genres']:
            units.append('Genres: '+', '.join(movie['genres'])+'.')
        if movie['themes']:
            units.append('Themes: '+', '.join(movie['themes'])+'.')
        units.extend(re.split(r'(?<=[.!?])\s+', movie['plot_summary']))
        groups = split_passages([u for u in units if u.strip()], count, limit)
        for n, group in enumerate(groups, 1):
            body = ' '.join(group)
            out.append({'show_id': movie['show_id'], 'passage_no': n, 'text': body,
                        'text_hash': hashlib.sha256(f'{identity}|{TEXT_VERSION}|{body}'.encode()).hexdigest(),
                        'token_count': count(body), 'title': fold(movie['title']) if n == 1 else '',
                        'categories': fold(' '.join(movie['genres']+movie['themes'])) if n == 1 else '',
                        'body': fold(body)})
    return out, limit


def activate(conn, generation):
    """Commit must be performed by the caller; activation is one transaction."""
    conn.execute('SELECT pg_advisory_xact_lock(78124497)')
    row = conn.execute('SELECT status FROM schema_v2_large.generation WHERE generation_id=%s',
                       (generation,)).fetchone()
    if not row or row[0] != 'ready':
        raise ValueError('Only a verified ready generation can be activated')
    old = conn.execute('SELECT generation_id FROM schema_v2_large.active_generation WHERE singleton FOR UPDATE').fetchone()
    previous = old[0] if old else None
    if previous == generation:
        return
    conn.execute('''INSERT INTO schema_v2_large.active_generation(singleton,generation_id,previous_generation_id)
        VALUES(TRUE,%s,%s) ON CONFLICT(singleton) DO UPDATE SET
        generation_id=EXCLUDED.generation_id, previous_generation_id=EXCLUDED.previous_generation_id, switched_at=now()''',
        (generation, previous))
    conn.execute('UPDATE schema_v2_large.generation SET activated_at=now() WHERE generation_id=%s', (generation,))


def rollback(conn):
    conn.execute('SELECT pg_advisory_xact_lock(78124497)')
    row = conn.execute('SELECT previous_generation_id FROM schema_v2_large.active_generation WHERE singleton FOR UPDATE').fetchone()
    if not row or row[0] is None:
        raise ValueError('No previous large-catalog generation')
    activate(conn, row[0])
    return row[0]


def initialize(conn):
    if not conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
        raise ValueError('pgvector is not enabled in this database. Ask its administrator to enable vector once.')
    conn.execute(SCHEMA_FILE.read_text(encoding='utf-8'))


def ingest(data, *, url=None, aliases_file=DEFAULT_ALIASES, dry_run=False, lexical_only=False,
           released_date_kind='platform', no_activate=False, embedder=None, passage_tokens=384):
    t0 = time.perf_counter()
    # Validate ALL records and aliases before model loading, connection or schema writes.
    movies = read_movies(data, released_date_kind)
    aliases, skipped = build_aliases(movies, aliases_file)
    report = {'schema': 'schema_v2_large', 'assets': len(movies), 'aliases': len(aliases),
              'alias_groups_absent_from_cast': sorted(set(skipped)),
              'missing_plot': sum(not m['plot_summary'] for m in movies),
              'missing_audio_languages': sum(not m['audio_languages'] for m in movies),
              'missing_primary_language': sum(m['primary_language'] is None for m in movies),
              'missing_original_release_date': sum(m['original_release_date'] is None for m in movies),
              'released_date_kind': released_date_kind,
              'primary_languages': sorted({m['primary_language'] for m in movies if m['primary_language']}),
              'full_snapshot_replacement': True}
    if dry_run:
        report.update(dry_run=True, validation='passed; no database or model accessed')
        return report
    if not lexical_only and embedder is None:
        embedder = load_embedder()
    if lexical_only:
        embedder = None
    docs, limit = passages(movies, embedder, passage_tokens)
    if embedder and embedder.profile.name == 'hash-test' and not os.getenv('LARGE_ALLOW_HASH_TEST'):
        raise ValueError('Hash embeddings are test-only; use lexical-only or a real model')
    vocab = Counter()
    for d in docs:
        vocab.update({w for w in re.findall(r'[^\W_]+', d['body']) if len(w) >= 2})
    corpus_hash = hashlib.sha256(json.dumps(sorted((m['show_id'], m['source_sha256']) for m in movies)).encode()).hexdigest()
    report.update(documents=len(docs), embedded=0, reused=0, corpus_sha256=corpus_hash,
                  max_tokens=max(d['token_count'] for d in docs), lexical_only=embedder is None,
                  aliases_sha256=hashlib.sha256(json.dumps(aliases, sort_keys=True).encode()).hexdigest())
    with psycopg.connect(url or database_url(), connect_timeout=5) as conn:
        # Serialize imports across processes without blocking readers. The session lock
        # covers first-time DDL too and is released automatically on disconnect.
        conn.execute('SELECT pg_advisory_lock(78124498)')
        initialize(conn)
        conn.commit()
        p = embedder.profile if embedder else None
        gen = conn.execute('''INSERT INTO schema_v2_large.generation
            (status,embedding_model,model_revision,pooling,query_prefix,text_version,passage_tokens)
            VALUES('building',%s,%s,%s,%s,%s,%s) RETURNING generation_id''',
            (p.name if p else None, embedder.revision if embedder else None,
             p.pooling if p else None, p.query_prefix if p else None, TEXT_VERSION, limit)).fetchone()[0]
        conn.commit()
        try:
            columns = list(movies[0])
            from psycopg import sql
            statement = sql.SQL('INSERT INTO schema_v2_large.asset_snapshot(generation_id,{}) VALUES(%(gen)s,{})').format(
                sql.SQL(',').join(map(sql.Identifier, columns)),
                sql.SQL(',').join(sql.Placeholder(c) for c in columns))
            with conn.cursor() as cur:
                cur.executemany(statement, [{**{k: Jsonb(v) if k in ('primary_cast','supporting_cast','source_metadata') else v
                                               for k, v in m.items()}, 'gen': gen} for m in movies])
                cur.executemany('''INSERT INTO schema_v2_large.alias_snapshot
                    (generation_id,actor_name,actor_norm,alias,alias_norm,requires_context,provenance,note)
                    VALUES(%(gen)s,%(actor_name)s,%(actor_norm)s,%(alias)s,%(alias_norm)s,%(requires_context)s,%(provenance)s,%(note)s)''',
                    [{**a, 'gen': gen} for a in aliases])
            vectors = {}
            if embedder:
                unique = {d['text_hash']: d['text'] for d in docs}
                hashes = list(unique)
                for s in range(0, len(hashes), 500):
                    for h, v in conn.execute('''SELECT DISTINCT ON(text_hash) text_hash,embedding::text
                        FROM schema_v2_large.search_doc WHERE text_hash=ANY(%s) AND embedding IS NOT NULL''',
                        (hashes[s:s+500],)):
                        vectors[h] = v
                report['reused'] = len(vectors)
                todo = [h for h in hashes if h not in vectors]
                for s in range(0, len(todo), 32):
                    batch = todo[s:s+32]
                    values = np.asarray(embedder.encode_docs([unique[h] for h in batch]))
                    if values.shape != (len(batch), 384) or not np.isfinite(values).all():
                        raise ValueError('Encoder returned invalid vectors')
                    if not np.allclose(np.linalg.norm(values, axis=1), 1, atol=1e-3):
                        raise ValueError('Encoder returned non-unit vectors')
                    vectors.update({h: vector_literal(v) for h, v in zip(batch, values)})
                    log.info('embedded %d/%d unique passages', min(s+32, len(todo)), len(todo))
                report['embedded'] = len(todo)
            with conn.cursor() as cur:
                cur.executemany('''INSERT INTO schema_v2_large.search_doc
                    (generation_id,show_id,passage_no,text,text_hash,token_count,tsv_main,embedding)
                    VALUES(%(gen)s,%(show_id)s,%(passage_no)s,%(text)s,%(text_hash)s,%(token_count)s,
                    setweight(to_tsvector('english',%(title)s),'A') ||
                    setweight(to_tsvector('english',%(categories)s),'B') ||
                    setweight(to_tsvector('english',%(body)s),'C'),%(vector)s::vector)''',
                    [{**d, 'gen': gen, 'vector': vectors.get(d['text_hash'])} for d in docs])
                with cur.copy('COPY schema_v2_large.lexicon(generation_id,word,df) FROM STDIN') as cp:
                    for word, df in vocab.items():
                        cp.write_row((gen, word, df))
            actual, embedded, covered = conn.execute('''SELECT count(*), count(embedding), count(DISTINCT show_id)
                FROM schema_v2_large.search_doc WHERE generation_id=%s''', (gen,)).fetchone()
            if (actual, covered, embedded) != (len(docs), len(movies), len(docs) if embedder else 0):
                raise ValueError('Generation coverage/vector verification failed')
            conn.execute('''UPDATE schema_v2_large.generation SET status='ready',asset_count=%s,doc_count=%s,notes=%s
                WHERE generation_id=%s''', (len(movies), len(docs), Jsonb(report), gen))
            if not no_activate:
                activate(conn, gen)
            conn.commit()
        except BaseException:
            conn.rollback()
            conn.execute("UPDATE schema_v2_large.generation SET status='failed' WHERE generation_id=%s", (gen,))
            conn.commit()
            raise
        finally:
            conn.execute('SELECT pg_advisory_unlock(78124498)')
            conn.commit()
    report.update(generation_id=gen, activated=not no_activate, seconds=round(time.perf_counter()-t0, 3))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data', type=Path)
    ap.add_argument('--aliases', type=Path, default=DEFAULT_ALIASES)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--lexical-only', action='store_true', help='No model needed; embedding column remains NULL')
    ap.add_argument('--released-date-kind', choices=['unknown','original','platform'], default='platform',
                    help='Default platform: released_date is ZEE5 arrival and supplies release_date')
    ap.add_argument('--no-activate', action='store_true')
    action = ap.add_mutually_exclusive_group()
    action.add_argument('--activate', type=int)
    action.add_argument('--rollback', action='store_true')
    action.add_argument('--init-only', action='store_true', help='Create only the six empty tables')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    try:
        if args.init_only or args.activate or args.rollback:
            with psycopg.connect(database_url(), connect_timeout=5) as conn:
                if args.init_only:
                    conn.execute('SELECT pg_advisory_xact_lock(78124498)')
                    initialize(conn)
                    result = {'schema': 'schema_v2_large', 'tables': 6, 'imported': False}
                elif args.rollback:
                    result = {'active_generation': rollback(conn)}
                else:
                    activate(conn, args.activate)
                    result = {'active_generation': args.activate}
        else:
            if not args.data:
                ap.error('--data is required for import')
            result = ingest(args.data, aliases_file=args.aliases, dry_run=args.dry_run,
                            lexical_only=args.lexical_only, released_date_kind=args.released_date_kind,
                            no_activate=args.no_activate)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (ValueError, OSError, psycopg.Error) as exc:
        # Database exception text may include credentials supplied by libpq. Redact it.
        print(f'ERROR: {exc.__class__.__name__}' if isinstance(exc, psycopg.Error) else f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
