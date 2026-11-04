# Catalog-only ingestion verification

Verified 4 November 2026 on Windows, Python 3.11.9, PostgreSQL 16 and pgvector 0.8.6.

Current version: **audio languages only**. The earlier optional-primary-language
fix recorded below is superseded. No primary-language column is present in the
current schema.

Scope: only the importer in `catalog_large/`, its fixed `schema_v2_large` DDL,
actor/actress alias seeds, tests and documentation. No new search algorithm,
in-memory loader, API routing or UI was implemented.

## Results

- Full Python invocation: **186 passed, 28 skipped**. This includes the 162
  existing database-independent tests, 19 new pure import tests and 5 new
  database import tests. The 28 existing `search_v2` database tests skipped
  because `TEST_DATABASE_URL` was deliberately unset.
- Integration URL used a disposable `catalog_large_ingest_test` database on the
  isolated review cluster at 127.0.0.1:55439. `LARGE_TEST_DATABASE_URL` is separate
  from the old test setting. `LARGE_TEST_REAL_MODEL=1` enabled the BGE test.
- Imported **2,000 synthetic movie records** in lexical-only mode. Verified exactly
  six schema tables, 2,000 metadata records, 2,000 searchable passages, actor-only
  aliases, distinct primary/audio languages, ZEE5 date mapping and lexical stemming.
- Sentinel records in `public` and `search_v2` remained unchanged.
- Staged replacement left the active generation unchanged; activation selected the
  new complete snapshot; rollback restored the original movie membership.
- Injected embedding failure left the prior generation active, rolled back new
  snapshot data and marked the attempted generation failed/non-activatable.
- Invalid required fields/duplicates rejected before database/schema writes.
- Real local BGE-small-en-v1.5 generated two 384-dimensional document vectors;
  repeated import reused both vectors with no new embedding work.
- CLI `--dry-run`, `--help`, idempotent `--init-only`, and example lexical import
  also executed using the actual V2 `.venv`, without installing development
  dependencies into it. CLI database writes used only the disposable test DB.

## Limits

The user's ~2,000-movie JSON is intentionally represented by `"set custom path"`.
It was not supplied or imported. The scale corpus above is synthetic; the BGE
test used two records and does not establish full-corpus embedding performance.
No application database import, production relevance benchmark or Mac execution
was performed. Existing `search_v2`, original catalog tables and V1 are unchanged.
The isolated review cluster was stopped after verification.

## Optional primary-language fix (4 November 2026)

Primary language is now optional and remains NULL when absent. Both audio field
spellings are preserved without guessing an original language. DDL upgrades the
initial NOT NULL constraint automatically. Additional regressions cover missing,
null and blank values, 2,000 audio-only records and an existing initial schema.

The updated publish checkout passed all **28 focused tests**, including real
BGE, on Windows with the disposable review database.

## Audio-only version

All 28 focused tests passed on Windows after removing primary language. This
includes 2,000 multi-audio movie records, the legacy-column upgrade, and real
BGE generation/reuse. The actual user export has not been supplied locally.
