# V2 on the Mac where V1 already works

**Reuse your existing PostgreSQL server, database and login. You do not need to reinstall PostgreSQL,
create another database/user, or change V1's tables.**

V2 needs its own Python environment, embedding model and search index **once**. After that:
**start the existing PostgreSQL server if stopped, then start V2 on port 8001.**

| Component | What to do for V2 |
|---|---|
| PostgreSQL/database/login | Reuse V1's working connection |
| pgvector | Check it is enabled in that same database; skip installation if present |
| V1 tables and environment | Leave unchanged |
| V2 tables/vectors | Created automatically by V2's importer in the separate `search_v2` schema |
| API | V2 on 8001; V1 can stay on 8000 |
| LLM server | V2 does not need V1's Qwen/llama.cpp server |

These manual steps are sufficient. **You do not need to run `build_v2.sh`.**

## 1. Prepare the V2 folder and Python environment — once

Copy the V2 source into a separate folder on the Mac. Do not copy the Windows `.venv/`,
`config/.env`, `config/calibration.json`, `runtime/` or Python caches. When updating an existing
Mac V2 installation, preserve its local configuration and valid calibration.

Replace the example project path below with your actual V2 folder:

~~~bash
cd "$HOME/Desktop/Text-Search-v2"
python3 --version                 # must be Python 3.11 or newer
python3 -m venv .venv             # first setup only
source .venv/bin/activate
python -m pip install -r requirements.txt
test -f config/.env || cp config/.env.example config/.env
~~~

If your suitable installed Python is called `python3.12`, use that instead of `python3` when
creating the environment. A working V1 alone does not establish its Python version.
Use V2's environment; do not install these dependencies into V1's environment.

Open **V2's** `config/.env` in an editor:

- Set `DATABASE_URL` to V1's existing working connection value, including the same host, port,
  username, password if required, and database. Copy only that value, not the entire V1 config.
- Keep `API_HOST=127.0.0.1`, `API_PORT=8001`, `DENSE_ENABLED=true`,
  `EMBED_MODEL=bge-small-en-v1.5` and `TEXT_VERSION=tv2-2`.
- Remove copied Windows paths, or replace them with Mac paths. The default model directory is
  inside V2. Leave `DENSE_MIN_SIMILARITY` unset until calibrated on your actual catalog.

Exported environment variables override `.env`; check them if the connection or port is unexpected.
Do not print/share connection URLs containing passwords.

## 2. Start your existing PostgreSQL server and check pgvector

If V1 can currently query its database, PostgreSQL is already running: **nothing to start**.
Otherwise use the same method you already use for V1:

- Existing Homebrew PostgreSQL 16: `brew services start postgresql@16`.
- Another installed Homebrew version: start that existing version instead.
- Existing Postgres.app: open it and start the existing server; do not initialize a new cluster.
- Remote database: ensure the existing remote server is reachable.

With V2's environment active, this checks the configured database read-only:

~~~bash
python - <<'PY'
import psycopg
from config.settings import settings

with psycopg.connect(settings.database_url, connect_timeout=5,
                     options="-c default_transaction_read_only=on -c statement_timeout=5000") as conn:
    print("Database:", conn.execute("SELECT current_database()").fetchone()[0])
    row = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
    print("pgvector:", row[0] if row else "NOT ENABLED in this database")
PY
~~~

If a version is printed, **skip all extension installation**. NULL vectors in V1's table do not mean
pgvector is missing.

Only if absent: use an administrator connection to **the same database** to run
`CREATE EXTENSION IF NOT EXISTS vector;` once. If `vector.control` is missing, install the extension
for the exact existing PostgreSQL version; do not reinstall/upgrade the working V1 server.
Current Homebrew pgvector targets PG17/18, so a plain `brew install pgvector` is not a reliable PG16
installation step. See [official pgvector installation guidance](https://github.com/pgvector/pgvector#installation).

The configured role must also be allowed to create/manage `search_v2`. For a permissions error,
ask the database owner for the required access instead of recreating the database.

## 3. Create V2 tables and vectors — once

Use the complete movie JSON export folder. The importer reads JSON; it does not extract movies
from V1's database tables. Before importing, check `config/import_profile.json` against the real data:

- Map original release dates/years and directors only if present; directors currently need a list of names.
- Do not substitute platform arrival dates for original release dates.
- Check content type: defaults infer it from GCS path segments `MOVIES`, `SHOWS`, `ORIGINALS`.
  Use the profile's field-based mapping if your source paths differ.

Get the complete embedding model, unless it is already present:

~~~bash
python -m scripts.download_model bge-small-en-v1.5
~~~

The ONNX file and `tokenizer.json` are portable, so copying the **complete matching model folder**
from Windows is also valid. Do not copy just the ONNX file. No GPU or LLM API is required.

Set the actual JSON folder path, then validate and import:

~~~bash
MOVIES="$HOME/Movies-data/output"  # replace with your actual complete JSON folder
python -m app.ingest --data "$MOVIES" --dry-run
python -m app.ingest --data "$MOVIES"
~~~

The dry run performs no database writes. Run the import after validation succeeds.
**The importer creates the schema/tables and builds/activates embeddings automatically.
No manual table-creation SQL is needed.**

V2 creates `search_v2.search_doc` (vectors and lexical indexes), generation-scoped asset/scene/alias
snapshots, generation tracking, vocabulary and active compatibility tables. It does not update
`public.cms` or `public.scene_embeddings`; V1's original embedding column may remain NULL.

Each import replaces the **complete V2 catalog**. Always supply the full folder; do not import one
movie expecting an incremental merge. Unchanged embeddings can be reused. Do not rebuild for daily startup.
If a valid current V2 index and matching model already exist on this Mac, skip this step.

Optional views `cms_v2` and `scene_embeddings_v2` are created by the build helper, not these manual
steps. The application queries underlying V2 tables directly and does not need those views.

## 4. Every time: start V2

Ensure the existing PostgreSQL server is running as in step 2. Then:

~~~bash
cd "$HOME/Desktop/Text-Search-v2"  # use your actual project folder
source .venv/bin/activate
python -m app.start
~~~

Open **http://127.0.0.1:8001/**. Keep the Terminal open. Ctrl+C stops V2's API, not PostgreSQL.
V1 can continue on port 8000. Do not stop the shared database while V1 needs it.
Restart V2 after source or configuration changes; code reload is not enabled.

Check **http://127.0.0.1:8001/api/ready**:

- `status: ready`, with your expected asset/document counts.
- `dense: true` and the correct embedding model.
- `dense_calibrated: false` is initially possible: startup works, but rejection quality still needs calibration.

Try your real titles, actors, genre/language listings, scenes and dialogue quotes, not the Windows
demo titles. Long source text may create multiple passages and missing dialogue may create none,
so document counts need not equal exactly two vectors per scene.

## 5. Later: update the real catalog

For changed movie files or import mappings, activate V2's environment and repeat the validation/import
commands from step 3 with the complete folder. The app checks for generation changes on subsequent
requests, normally within about two seconds. Restart V2 after code/configuration changes.

Rollback, only when you want the previous ready generation:

~~~bash
python -m app.ingest --rollback
~~~

Calibration is optional for startup but needed to validate rejection quality. This deployment repository
excludes the development evaluation tools and demo query sets. Do not use a demo cutoff for your real
20 movies. If a separately produced calibration file is provided, place it at config/calibration.json
and restart V2; readiness reports whether its identity matches the active index. Do not assume a
single cutoff guarantees perfect accuracy.

## Troubleshooting

| Symptom | Check |
|---|---|
| Connection / pool timeout | Existing PostgreSQL is running and V2 uses V1's working connection. |
| Missing Python packages | Activate V2's environment and install requirements.txt there. |
| No active generation / snapshot missing | Validate/import the complete JSON folder in step 3. |
| Missing model/tokenizer / dense false | Read dense_reason; download the complete configured model. Rebuild only for an index/model mismatch. |
| Port 8001 busy | Check whether V2 is already running, or choose another free API_PORT and restart. |
| Years/directors unavailable | Map the actual source fields in config/import_profile.json; embeddings do not supply missing metadata. |
| Video does not play | Configure Mac GCS credentials with bucket access; text search does not need them. |

The commands have not been executed on your Mac. No fresh database installation is needed when
V1's existing PostgreSQL server and pgvector already meet the requirements.
