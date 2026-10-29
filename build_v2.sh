#!/usr/bin/env bash
# build_v2.sh - one-time setup of Text Search V2 on macOS (safe to run again).
#   bash build_v2.sh /path/to/folder-with-Movie_json-files
# Optional: DATABASE_URL=postgresql://user@127.0.0.1:5432/dbname bash build_v2.sh /path/to/movies
# Touches only: the search_v2 schema in that database, and .venv / models / config/.env in this folder.
set -euo pipefail

V2="$(cd "$(dirname "$0")" && pwd)"
MOVIES="${1:-}"
if [ -z "$MOVIES" ] || [ ! -d "$MOVIES" ]; then
  echo "Usage: bash build_v2.sh /path/to/folder-with-Movie_*.json"; exit 1
fi
MOVIES="$(cd "$MOVIES" && pwd)"
cd "$V2"
step() { printf '\n\033[36m== %s\033[0m\n' "$*"; }

step "1/7  Python 3.11+"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then echo "Python 3.11 or newer not found. Install it:  brew install python@3.12"; exit 1; fi
"$PY" --version

step "2/7  Python environment (.venv)"
[ -x "$V2/.venv/bin/python" ] || "$PY" -m venv "$V2/.venv"
VPY="$V2/.venv/bin/python"
"$VPY" -m pip install --disable-pip-version-check -q -r requirements.txt

step "3/7  config/.env"
[ -f config/.env ] || { cp config/.env.example config/.env; echo "created from .env.example"; }
if [ -n "${DATABASE_URL:-}" ]; then
  "$VPY" - "$DATABASE_URL" <<'PY'
import re, sys, pathlib
p = pathlib.Path("config/.env"); s = p.read_text()
s, replaced = re.subn(r"(?m)^DATABASE_URL=.*$", lambda m: "DATABASE_URL=" + sys.argv[1], s)
if not replaced: s += "\nDATABASE_URL=" + sys.argv[1] + "\n"
p.write_text(s); print("DATABASE_URL set in config/.env")
PY
fi
# Preserve local calibration; the application validates model/text/corpus identity.

step "4/7  Database and pgvector"
"$VPY" - <<'PY'
import sys, psycopg
from config.settings import settings
try:
    conn = psycopg.connect(settings.database_url, connect_timeout=5, autocommit=True)
except Exception as e:
    sys.exit(f"Cannot connect to the configured database ({type(e).__name__}).\n  Is PostgreSQL running? (brew services start postgresql@16)  See MAC.md step 2.")
with conn:
    row = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
    if row is None:
        try:
            conn.execute("CREATE EXTENSION vector")
            row = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        except Exception as e:
            sys.exit(f"pgvector is not enabled and could not be created:\n  {e}\n  Run once as a superuser: CREATE EXTENSION vector;  (MAC.md step 2)")
    print(f"connected to configured database; pgvector {row[0]}")
PY

step "5/7  Embedding model bge-small-en-v1.5 (about 130 MB, once)"
if [ -f models/bge-small-en-v1.5/onnx/model.onnx ] && [ -f models/bge-small-en-v1.5/tokenizer.json ]; then echo "already downloaded"
else "$VPY" -m scripts.download_model bge-small-en-v1.5; fi

step "6/7  Check the source files, then build the index from $MOVIES"
"$VPY" - "$MOVIES" <<'PY'
import json, sys, collections, pathlib
files = sorted(pathlib.Path(sys.argv[1]).glob("*.json"))
top, cms = collections.Counter(), collections.Counter()
for f in files:
    try: d = json.loads(f.read_text(encoding="utf-8"))
    except Exception: continue
    top.update(d.keys()); cms.update((d.get("cms_metadata") or {}).keys())
print(f"{len(files)} JSON files")
hint = ("date", "year", "director", "popular", "rating", "views", "rank", "type", "category")
found = [f"cms_metadata.{k}" for k in cms if any(h in k.lower() for h in hint)] + [k for k in top if any(h in k.lower() for h in hint)]
print("Fields that could be mapped in config/import_profile.json:", ", ".join(sorted(found)) or "none")
prof = json.loads(pathlib.Path("config/import_profile.json").read_text())
print("Currently mapped:", {k: (v.get("field") if isinstance(v, dict) else v) for k, v in prof.items() if not k.startswith("_")})
PY
"$VPY" -m app.ingest --data "$MOVIES" --dry-run
"$VPY" -m app.ingest --data "$MOVIES"

step "7/7  Named views + check"
"$VPY" - <<'PY'
import psycopg
from config.settings import settings
with psycopg.connect(settings.database_url, autocommit=True) as c:
    c.execute("""CREATE OR REPLACE VIEW search_v2.cms_v2 AS
      SELECT a.* FROM search_v2.asset_snapshot a
      WHERE a.generation_id = (SELECT generation_id FROM search_v2.active_generation)""")
    c.execute("""CREATE OR REPLACE VIEW search_v2.scene_embeddings_v2 AS
      SELECT d.doc_id, d.asset_id, d.language, d.asset_key, d.scene_id, d.kind, d.passage_no, d.text, d.embedding
      FROM search_v2.search_doc d
      WHERE d.scope = 'scene' AND d.generation_id = (SELECT generation_id FROM search_v2.active_generation)""")
    print("titles (cms_v2):", c.execute("SELECT count(*) FROM search_v2.cms_v2").fetchone()[0])
    for kind, n, v in c.execute("SELECT kind, count(*), count(embedding) FROM search_v2.scene_embeddings_v2 GROUP BY kind ORDER BY kind"):
        print(f"scene_embeddings_v2 ({kind}): {n} rows, {v} with a vector")
PY
printf '\n\033[32mDone. Start the app:  source .venv/bin/activate && python -m app.start   ->  http://127.0.0.1:8001\033[0m\n'
