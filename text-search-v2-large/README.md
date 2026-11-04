# Text Search V2 Large — movie metadata only

A separate project copied/adapted from Text-Search-v2. The original V1 and V2
folders, their configuration and their application tables are unchanged.

In this GitHub repository, this project lives in **`text-search-v2-large/`**.
After pulling/downloading, enter that subfolder before running commands. The
standalone transfer ZIP instead extracts a `Text-Search-v2-Large/` project folder.

This app serves the **existing `schema_v2_large` catalog**: movie titles, plots,
genres, themes, primary/supporting cast, actor aliases, directors, available audio
languages and ZEE5 release dates. It has no scene, dialogue, transcript or playback
endpoint. It does not store or infer a primary/original language. There is no LLM
in the request path. Default API/UI port: **8002**.

## Run on the Mac with the populated database

See [MAC.md](MAC.md) for exact steps. No reimport is required merely to connect to
the catalog already imported with the matching BGE model. API startup is read-only.

```bash
cd "/path/to/Text-Search-v2-Large"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export LARGE_DATABASE_URL='postgresql://YOUR_USER:YOUR_PASSWORD@127.0.0.1:5432/YOUR_DATABASE'
export EMBED_MODEL=bge-small-en-v1.5
export MODELS_DIR="/parent/directory/containing/bge-small-en-v1.5"
python -m app.start
```

Open http://127.0.0.1:8002/ and check http://127.0.0.1:8002/api/ready.

## Request flow and inherited algorithms

1. Load one coherent active-generation snapshot of metadata, aliases and lexicon
   into memory. Poll the generation pointer every two seconds; rebuild only when
   the pointer changes. Do not reread all movie JSON files per request.
2. Use V2's normalized entity gazetteer, longest spans, transliteration skeleton,
   bounded edit distance, explicit constraints/negation, dates, sorts and limits. Partial-name typo candidates are indexed by trigrams/skeleton; audio/type membership and immutable coverage are precomputed per snapshot.
3. Exact title/metadata listings return from memory. Other plot/preference requests
   run two constrained channels concurrently: `catalog_lex` and `catalog_dense`.
4. Lexical search uses native PostgreSQL English FTS, staged AND/all-but-one/minimum
   match, GIN candidates and ts_rank_cd. Controlled synonym alternatives are ORed
   within a term rather than made additional mandatory words.
5. Dense search uses the matching normalized BGE vector and exact inner product.
   Passage scores are aggregated per movie before the movie window. This intentionally
   avoids a fixed raw-passage pool crowding out short movies at this catalog scale;
   it is not currently an ANN/HNSW query. Benchmark before changing that policy.
6. Weighted RRF combines ranks (not probabilities). Explicit date sorting remains
   authoritative after optional reranking. Reranker defaults off and is not model-
   benchmarked here. Missing/failed channels produce labelled degraded results.

Request deadline, statement timeouts, bounded inference admission, query-embedding
LRU and cancellation ownership are inherited from V2. Each request pins its
generation for filters and retrieval, including when another request refreshes
the current catalog. Metadata serving uses only SELECTs/read-only transactions.

## Title priority

A complete title wins first. For a concise title/listing request, contiguous
whole-word fragments of a title also count as title candidates, ahead of an
incidental character or cast-name match. A word-to-title posting index is built
from the active catalog; requests intersect those postings rather than scanning
all movie records. There are no movie-specific exceptions or suffix rules.

Explicit person requests preserve their person meaning. Known genre/language/theme
queries and ordinary plot descriptions retain their existing behavior. Common-word
checks account for words already occurring in titles and do not demote a known
person name below a competing partial title. If several titles match, they remain
alternatives rather than selecting an arbitrary winner. Debug traces record the
selected title-fragment match. Pull this code and restart; no data reimport,
title rename or embedding rebuild is required for this priority correction.

## Language and date semantics

- `Tamil movies`: movies with Tamil in `audio_languages`.
- `Tamil and Hindi movies`: both tracks must be available.
- `Tamil or Hindi movies`: either track may be available.
- `Tamil dubbed movies`: Tamil availability; a note explains that dubbed versus
  original status is unknown. Explicit original-language requests clarify.
- `latest`, `before 2020`, etc. use `release_date`, which was declared to mean ZEE5
  arrival for this catalog. Do not describe it as theatrical release.
- Missing director/character information remains unknown; embeddings do not fill it.

## Synonyms and preferences

Edit **[app/vocab.py](app/vocab.py)** and restart. It is the single source for
`GENRE_SYNONYMS`, `MOOD_PREFERENCES`, and `LEXICAL_SYNONYM_GROUPS`. The lexical lookup
map is generated from the groups in that same module. There is no separate JSON
synonym registry or loader.

The planner combines this editable vocabulary with names/categories dynamically
loaded from the database. It does not automatically learn from queries. Existing V2 groups are retained.
New groups include family, funny/comedy, love story/romance, animation, biography,
and documentary; descriptive preferences include feel-good, heartwarming, uplifting,
lighthearted, friendship, revenge, emotional and inspirational wording.

Known genre/theme labels can be exact constraints. Mood expressions without a
matching label are soft plot/theme preferences, labelled approximate; they do not
invent a Family/Comedy label or promise a happy ending. Family genre is distinct
from validated child suitability; child-safety requests clarify because appropriate
age/content data was not provided. Negated soft moods also clarify instead of being
silently converted into positive search.

Actor aliases come only from the generation's `alias_snapshot`, not movie aliases.
Canonical names/titles take precedence over nicknames. Ambiguous nickname targets
clarify. Broad/role labels honor `requires_context`; role-based custom aliases are
search mappings, not claims about current officeholders.

Ordinary-word typo recovery is conservative: unknown 5+ letter lexical words may
change only when exactly one lexicon candidate is one edit away, after name checks.
Original semantic wording stays intact. This is not unrestricted spell correction.

## Storage and ingestion

The existing six tables remain: `generation`, `active_generation`, `asset_snapshot`,
`alias_snapshot`, `search_doc`, `lexicon`. Readiness reports the schema and counts.
No table migration or import occurs on API startup.

The included ingestion module is the tested audio-only importer:

```bash
MOVIES_JSON="set custom path"
python -m catalog_large.ingest --data "$MOVIES_JSON" --dry-run
python -m catalog_large.ingest --data "$MOVIES_JSON"
```

Only use these commands for deliberate data updates. An import replaces the full
catalog snapshot. Actor-alias changes require a complete reimport; query synonym
changes in app/vocab.py only require an application restart. Existing vector/text
hashes are compatible; no rebuild is needed solely for this integration.

## API and diagnostics

`GET /api/health`, `GET /api/ready`, `POST /api/search` with
`{"query":"feel-good films","limit":10,"debug":true}`. Result cards contain
show_id, plot, cast relationships, themes, genres, audio languages and release date.
No match-percentage score is returned.

CLI: `python -m scripts.query --query "Thalapathy movies"`.
Add `--lexical-only` to that CLI for a lexical comparison. The API still uses both
channels unless `DENSE_ENABLED=false`. That setting labels affected search responses
degraded; title/listing paths do not require the encoder.

Run `python -m scripts.loadtest --url http://127.0.0.1:8002 --queries YOUR_QUERIES.json`
with a JSON list containing `{"text":"..."}` entries. The warm p95 gate also requires
zero HTTP errors and zero degraded responses. The first distinct-query pass is
not necessarily process-cold.

## Tests and limits

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

Database cases require a disposable `LARGE_TEST_DATABASE_URL` with `test` in the
database name. Those tests reset `schema_v2_large` there. Never point it at your
working movie database. `LARGE_TEST_REAL_MODEL=1` enables a real BGE import test.

See [VERIFICATION.md](VERIFICATION.md) for recorded results. Synthetic fixtures
verify behavior and local scale, not production relevance. The actual 2,357-movie
export exists only on the user's Mac and was unavailable for Windows testing.
Dense retrieval without real-catalog calibration can return loosely related
neighbors; a passing software suite does not establish rejection accuracy.
