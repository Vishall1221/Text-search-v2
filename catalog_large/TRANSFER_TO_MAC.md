# Mac transfer package

The `Text-Search-v2-Large.zip` package contains only ingestion code and its
dependencies from this project. Extract it to create `Text-Search-v2-Large/`.
It has no credentials, Windows virtual environment, source movie export or model
weights. A complete matching BGE model folder can be copied separately, or
downloaded with the command below.

Assuming you extracted the folder onto your Desktop:

```bash
cd "$HOME/Desktop/Text-Search-v2-Large"
python3 --version   # Python 3.11+ required
python3 -m venv .venv-large
source .venv-large/bin/activate
python -m pip install -r catalog_large/requirements.txt

export LARGE_DATABASE_URL='postgresql://YOUR_USER:YOUR_PASSWORD@127.0.0.1:5432/YOUR_DATABASE'
export EMBED_MODEL=bge-small-en-v1.5
export MODELS_DIR="$PWD/models"
MOVIES_JSON="set custom path"

python -m catalog_large.ingest --data "$MOVIES_JSON" --dry-run
python -m scripts.download_model bge-small-en-v1.5
python -m catalog_large.ingest --data "$MOVIES_JSON"
```

Replace the database connection and `set custom path` with your existing Mac
database connection and complete movie JSON path. Start your existing PostgreSQL
server first. The selected database must already have pgvector enabled and the
user must be permitted to create schema_v2_large. The importer creates its six
tables automatically. Do not supply `--lexical-only` when embeddings are wanted.

The default `released_date` meaning is ZEE5 arrival, as agreed. No schema name
setting is necessary: the importer is fixed to schema_v2_large. Reimporting a
single file replaces the entire active catalog with that file's movies. Always
give the complete export. See `catalog_large/mac.md` for full details, alias
configuration, staging and rollback.

Read-only verification after import:

```bash
python - <<'PY'
import psycopg
from catalog_large.ingest import database_url
with psycopg.connect(database_url(), connect_timeout=5,
                     options='-c default_transaction_read_only=on -c statement_timeout=5000') as conn:
    gid = conn.execute('SELECT generation_id FROM schema_v2_large.active_generation').fetchone()[0]
    movies = conn.execute('SELECT count(*) FROM schema_v2_large.asset_snapshot WHERE generation_id=%s', (gid,)).fetchone()[0]
    docs, vectors = conn.execute('SELECT count(*),count(embedding) FROM schema_v2_large.search_doc WHERE generation_id=%s', (gid,)).fetchone()
    aliases = conn.execute('SELECT count(*) FROM schema_v2_large.alias_snapshot WHERE generation_id=%s', (gid,)).fetchone()[0]
    print(dict(schema='schema_v2_large', generation=gid, movies=movies,
               documents=docs, vectors=vectors, actor_aliases=aliases))
    assert docs > 0 and docs == vectors, 'Not every active document has an embedding'
PY
```

On future terminal sessions, activate `.venv-large` again and set the same
connection/model environment variables before importing. The package was smoke-
tested on Windows in isolation; it has not been executed on a Mac.
