# Text Search V2

English movie search for titles, actors, genres, storylines, scenes and dialogue quotes.
Combines PostgreSQL full-text search with dense vector retrieval and weighted reciprocal rank fusion.
There is no LLM in the normal request path. Native PostgreSQL full-text ranking is not BM25.

This repository contains the runtime application and setup tools. Development tests, evaluation/demo
query sets, agent handoff documents, model binaries, movie exports and machine-local configuration
are deliberately excluded.

**Already running V1 on a Mac? Follow [MAC.md](MAC.md).** Reuse the existing PostgreSQL server and
database; V2 manages its own `search_v2` schema and does not modify V1's original tables.

## Requirements

- Python 3.11+; use a separate V2 virtual environment.
- PostgreSQL 16 with pgvector enabled in the selected database. An existing compatible server can be reused.
- Movie JSON exports in the expected format; configure real field mappings before import.
- BGE-small-en-v1.5 model files, downloaded once. Embeddings and search run locally on CPU.
- GCS credentials only if you want playback from private GCS video URLs.

## Setup

Clone this repository and enter its folder:

```bash
git clone https://github.com/Vishall1221/Text-search-v2.git
cd Text-search-v2
```

macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
test -f config/.env || cp config/.env.example config/.env
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path config/.env)) { Copy-Item config/.env.example config/.env }
```

Edit `config/.env` locally. Set `DATABASE_URL` to your existing working PostgreSQL connection;
keep `API_PORT=8001` to run alongside V1 on 8000. Do not commit passwords or credentials.
The application role needs permission to manage the V2 schema. Enable pgvector once as a database
administrator if it is not already installed. Do not recreate an existing V1 database.

Inspect `config/import_profile.json` against the real JSON exports. Original release dates/years and
directors are unmapped by default; map them only when present. Content type defaults to GCS path
segments `MOVIES`, `SHOWS`, `ORIGINALS`, with a field-based alternative available. The importer
expects directors as a list of names. It does not infer unavailable popularity or ratings.

Download the complete model, then validate and import your actual full source folder:

```bash
python -m scripts.download_model bge-small-en-v1.5
python -m app.ingest --data "/path/to/complete/movie-json-folder" --dry-run
python -m app.ingest --data "/path/to/complete/movie-json-folder"
```

The importer automatically creates tables, builds lexical documents and 384-dimensional vectors,
and activates a new generation. No manual table creation is required. It reads JSON exports rather
than extracting records from V1's database tables. An import **replaces the full V2 catalog**;
omitted assets are absent from the new snapshot. Unchanged document embeddings can be reused.

## Run

Start your existing PostgreSQL server using its normal startup method, then:

```bash
python -m app.start
```

Open **http://127.0.0.1:8001/**. Keep the terminal open; Ctrl+C stops the API.
Windows users can alternatively run `powershell -ExecutionPolicy Bypass -File .\run_v2.ps1`
to start the existing local PostgreSQL cluster and V2 in the background. That launcher checks
readiness, writes logs under `runtime/`, and does not rebuild the index.

Optional build helpers: `build_v2.sh` / `build_v2.ps1` install dependencies and import data;
they are not daily startup commands. The manual Mac instructions are in [MAC.md](MAC.md).

## API and storage

- `GET /api/health`: process liveness.
- `GET /api/ready`: database/catalog readiness, active generation, model and calibration status.
- `POST /api/search`: JSON `{"query":"your movie or scene query","limit":10}`.
- `GET /api/video/{asset_id}?language=...`: optional GCS playback proxy.

The planner resolves structured constraints before retrieval. Exact-title/browse paths use the
catalog snapshot; other modes run applicable lexical/vector/dialogue channels concurrently.
Results expose interpretation, notes and statuses: `ok`, `approximate`, `empty`,
`needs_clarification`, or `degraded`. Ranking scores are not match percentages.

`search_v2.search_doc` stores vectors and lexical representations. Metadata, aliases and scenes
are snapshot-scoped; activation and rollback select matching evidence and documents. V2 does not
backfill `public.scene_embeddings`. Optional `cms_v2` / `scene_embeddings_v2` views created by the
build helpers are convenience views, not application dependencies.

## Updates and rollback

Reimport the complete source folder after data/profile/model changes. The running service checks
generation changes on later requests. Restart after code or environment configuration changes.

```bash
python -m app.ingest --data "/path/to/complete/movie-json-folder" --no-activate
python -m app.ingest --activate 2  # replace 2 with the actual ready generation ID
python -m app.ingest --rollback   # only when rollback is intended
```

Pre-snapshot V2 indexes require a complete reimport; historical metadata cannot be reconstructed
automatically. No import or table migration occurs at API startup.

## Accuracy and calibration

The application can run without a calibrated cutoff, but dense search may return approximate
nearest neighbours for requests outside the catalog. `dense_calibrated: false` is a quality-validation
signal, not a failed startup. Verify relevance on the real 20-movie catalog before accepting it.

No demo cutoff or evaluation suite is shipped in this runtime repository. A separately produced
`config/calibration.json` can be loaded locally, or use `CALIBRATION_FILE` for another path. Stored
calibration must match the model revision, pooling, query prefix, text version, corpus hash and
passage settings. Stale cutoffs are ignored with a readiness reason. Do not assume a single cutoff
guarantees perfect accuracy.

Optional reranking is off by default. Search is English-only and single-query; conversational memory,
unsupported metadata and million-scene performance are not guaranteed by this release.

## Layout

```text
app/         planner, importer, embedding, retrieval, API and playback
config/      settings, import profile, aliases, .env.example
db/          V2 schema
frontend/    static HTML, CSS and JavaScript
scripts/     model downloader
README.md    runtime setup and architecture
MAC.md       setup for a Mac with working V1
```
