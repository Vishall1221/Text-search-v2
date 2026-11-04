# Catalog-only ingestion on Mac: schema_v2_large

This is **ingestion only**. It creates/imports six tables in `schema_v2_large` in
your existing PostgreSQL database. It does not change V1, `public` catalog tables,
`search_v2`, the existing API, in-memory query matching or the search algorithm.
The supplied example is fictional test data. The real ~2,000-movie file is not
included; replace **set custom path** below with its actual path on your Mac.

## 1. Copy the code and prepare Python

Clone or update this repository on the Mac. The importer needs `catalog_large`,
`app`, `config`, and `scripts` (for the model downloader). For a fresh checkout:

```bash
cd "$HOME/Desktop"
git clone https://github.com/Vishall1221/Text-search-v2.git
cd Text-search-v2
```

If already cloned, run `git pull --ff-only` in that checkout. The importer reuses
existing text/tokenizer/model utilities.
Do not copy a Windows virtual environment or machine-specific `.env` over your
Mac configuration. Keep your existing database, user and password.

From the project root (replace this project path if different):

```bash
cd "$HOME/Desktop/Text-search-v2"
python3 --version                 # Python 3.11 or newer
python3 -m venv .venv-large       # once; isolated ingestion environment
source .venv-large/bin/activate
python -m pip install -r catalog_large/requirements.txt
```

An existing V2 environment with these dependencies is also sufficient.

## 2. Reuse your existing PostgreSQL connection

Start your existing PostgreSQL server in the same way you use it for V1/V2. Do
not initialize a new cluster or reinstall a working server.

The importer uses `LARGE_DATABASE_URL`, falling back to V2's existing
`DATABASE_URL` in the environment or `config/.env`. To override for this terminal:

```bash
export LARGE_DATABASE_URL='postgresql://YOUR_USER:YOUR_PASSWORD@127.0.0.1:5432/YOUR_EXISTING_DATABASE'
```

Replace that example with your actual connection, or omit the export when V2's
connection is already correct. Never commit real credentials. The script does not
print the connection URL, and database errors omit potentially sensitive text.

The selected database must already have pgvector enabled, even for lexical-only
imports, because the schema has a nullable `vector(384)` column. If absent, an
administrator must enable it once in that same database:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The importer checks for the extension but does not install extensions or modify
tables outside `schema_v2_large`. The configured role needs permission to create
that schema and its tables. No manual table-creation commands are needed.

## 3. Check the JSON before importing

```bash
MOVIES_JSON="set custom path"
python -m catalog_large.ingest --data "$MOVIES_JSON" --dry-run
```

Accepted input: a JSON array of movies, `{"movies": [...]}`, one movie object, or
a folder of `.json` files. Folder imports read all top-level JSON files, so keep
alias/config files outside that data folder. All movies are validated before
database writes. UTF-8 and UTF-8 with BOM are accepted.

Required fields: `show_id`, `title`, `content_type`. `primary_language` is optional:
if absent, null or blank, it is stored as NULL and reported by the dry run.
Available audio languages are preserved; primary language is never inferred from
the first audio track. Other unknown metadata may also be null/absent. Each show_id must be unique across the entire
input. Put multiple dubbed tracks in `AudioLanguages`/`audio_languages`; do not
duplicate the movie record for each track. Lists are preferred; comma/semicolon/
pipe/slash-separated strings are also accepted for languages/genres/directors.
Cast must be arrays of objects with `actor_name` and optional `character_name`.
The original JSON is retained in `source_metadata`.

**Date agreement:** `released_date` means the ZEE5 release date here. By default
it populates `source_released_date`, `platform_release_date`, and `release_date`.
The last field is the intended date for relative/date-filter queries in this
catalog. `original_release_date` remains NULL unless supplied explicitly. Dates
must be YYYY-MM-DD. `--released-date-kind original` is available for other exports;
`unknown` retains the source date without choosing an effective release date.
This script stores dates; it does not implement query/date filtering.

`--dry-run` loads neither a model nor a database connection. It reports movie
counts, missing descriptions/audio/dates, active alias rows and actor alias groups
not found in this export. For an illustrative format:

```bash
python -m catalog_large.ingest --data catalog_large/example_movies.json --dry-run
```

Do not import the example into your working catalog: imports replace a complete
catalog snapshot, not just the movies changed since the previous import.

## 4. Import: choose lexical-only or embeddings

Lexical-only needs no model download:

```bash
python -m catalog_large.ingest --data "$MOVIES_JSON" --lexical-only
```

This creates all six tables, metadata, aliases, searchable passages, English full-
text vectors/GIN index and lexicon. The embedding column remains NULL.

For BGE embeddings as well, download the local model once and import without
`--lexical-only`:

```bash
python -m scripts.download_model bge-small-en-v1.5
EMBED_MODEL=bge-small-en-v1.5 python -m catalog_large.ingest --data "$MOVIES_JSON"
```

Default BGE runs on CPU, generates normalized 384-dimensional vectors and counts
the final input tokens before splitting long plots. Complete model files already
copied from Windows can be reused; the Python environment cannot. Model loading
does not call an LLM or a paid generation API. Unchanged passage/model hashes
reuse existing vectors inside `schema_v2_large`. Old V2's demo calibration is
not copied, used or changed by this importer.

## 5. Actor/actress aliases

Edit `catalog_large/actor_aliases.json` and reimport the complete movie source.
Use `--aliases /path/to/another.json` to choose a different alias file. Entries
are grouped by exact catalog actor names, not movie titles. Example:

```json
{
  "actors": [
    {
      "catalog_names": ["Vijay", "Joseph Vijay"],
      "aliases": ["Thalapathy", "Thalapthy", "Ilaya Thalapathy"],
      "provenance": "custom"
    }
  ]
}
```

The importer activates an alias group only when one of its normalized
`catalog_names` occurs in primary or supporting cast. It does not match
`Vijay` to `Vijay Sethupathi` by substring. Adjust catalog_names if your export
uses another spelling. Aliases are stored once per actor per generation, not
once per movie. Collisions retain all candidate actors instead of choosing one.

Seeds include the requested Vijay, Vadivelu, Suriya/Surya and Salman Khan labels,
plus a small editable set for Rajinikanth, Kamal Haasan, Shah Rukh Khan and
Nayanthara. They are search labels, not claims of exclusive ownership of a nickname.
`CM of Tamilnadu` and variants are explicitly user-supplied custom role labels;
they are not verified claims about the current officeholder. Review such labels
when roles change. `requires_context` is stored for broad/role-based labels.

Both metadata and actor aliases can subsequently be loaded into the application's
in-memory catalog. **This ingestion task does not add that loader or alias-query
behavior to the old API.** A later search integration must honor ambiguity and
requires_context; storing a nickname alone does not implement fuzzy lookup.

Reference examples for additional curated search labels:
[Sun Pictures' Rajinikanth page](https://www.sunpictures.in/casts/rajinikanth/),
[Red Chillies' SRK article](https://www.redchillies.com/news/shah-rukh-khan-has-always-been-active-and-there-for-us-red-chillies-entertainment-coo-on-srks-comeback).

## 6. Check the imported tables

Using your existing database client:

```sql
SELECT g.* FROM schema_v2_large.generation g
JOIN schema_v2_large.active_generation a USING (generation_id);

SELECT count(*) FROM schema_v2_large.asset_snapshot
WHERE generation_id=(SELECT generation_id FROM schema_v2_large.active_generation);

SELECT count(*) AS documents, count(embedding) AS vectors
FROM schema_v2_large.search_doc
WHERE generation_id=(SELECT generation_id FROM schema_v2_large.active_generation);

SELECT actor_name, alias, requires_context
FROM schema_v2_large.alias_snapshot
WHERE generation_id=(SELECT generation_id FROM schema_v2_large.active_generation)
ORDER BY actor_name, alias;
```

Other tables: `lexicon`, plus the two generation tables above. No scenes, dialogue
transcripts or playback URIs are required. About 2,000 movies need not mean exactly
2,000 passages: a long plot may split, and identical passages share embedding work.

## 7. Staging, updates and rollback

Normal import publishes only after verification; failed builds leave the old
active snapshot unchanged. To build without switching:

```bash
python -m catalog_large.ingest --data "$MOVIES_JSON" --lexical-only --no-activate
python -m catalog_large.ingest --activate 2   # replace 2 with the reported ID
python -m catalog_large.ingest --rollback
```

Always use the complete source. Old generations are retained; no automatic delete
is performed. To create only empty tables, use `--init-only`. The existing V2 API
still reads `search_v2`; importing here does not switch that API to the new schema.

## 8. Tests and platform status

From the project root, install the focused import test dependencies:

```bash
python -m pip install -r catalog_large/requirements-test.txt
python -m pytest -c catalog_large/pytest.ini
```

Database tests require `LARGE_TEST_DATABASE_URL` pointing at a separate disposable
database whose name contains `test`. They drop **schema_v2_large** there. Never use
your working database. `LARGE_TEST_REAL_MODEL=1` opts into the local BGE test.

The code and PostgreSQL tests were run on Windows; these Mac instructions have
not been executed on a Mac. The real ~2,000-movie export was not available locally;
the scale fixture is synthetic. See `VERIFICATION.md` for recorded checks.
