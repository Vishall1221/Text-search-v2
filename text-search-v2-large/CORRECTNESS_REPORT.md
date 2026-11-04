# Catalog search correctness investigation — 4 November 2026

The implementation improves typed entity resolution, spelling recovery, listing
order and lexical evidence preservation. It does not establish production-wide
accuracy or calibrate semantic relevance on the unavailable Mac catalog.

## Scope and reproducibility

Read the full root CHATGPT.md and CLAUDE.md. The baseline is local commit
`082d7e5`, including the previous title-fragment correction. The user's expanded
CHATGPT.md was already an uncommitted input and was not edited during this work.
Original V1/V2 folders and the application database were not changed.

The actual 2,357-movie export remains on Mac. All film facts below are **synthetic
fixtures representing the reported relationships**, not independently verified film
credits. The fixed evaluation has 28 movies and 63 judgments: 50 title/person
result-set judgments and 13 interpretation/safety checks. Judgments were frozen
before planner changes; their SHA-256 is
`63c39c058385c00d4ca5784c0abd9316c181a6f9b23c4dc2879c63c7714178a1`.

The before API ran from an isolated git archive of 082d7e5; the after API ran the
modified source. Both used identical fixture/alias hashes, PostgreSQL 16 on the
disposable test database at port 55439, and the same real BGE-small-en-v1.5 ONNX
encoder: revision 828e1496d7fabb79, CLS pooling, 384 dimensions and the same query
prefix. The API was exercised through HTTPX ASGI transport, including startup,
request validation, planner, real FTS/dense retrieval and response serialization.
These were not public-network HTTP load tests.

## Confirmed causes and changes

| Component | Verified defect | Change |
|---|---|---|
| planner._pick_entities / _finalize | An actor-labelled facet retained actors, directors and characters, then unioned all their movies. Even explicit actor cues admitted characters. | Select one typed relationship. Explicit actor/director/character/title slots never fall back to another kind. |
| gazetteer.Catalog alias loading | Any exact non-alias entity, including a character, suppressed the actor alias from exact and fuzzy indexes. | Preserve typed alias evidence through collisions; resolve it using context afterward. Canonical actor names retain priority over another actor's nickname. |
| planner role grammar | `character named X` was not a role cue; its grammar became residual plot text. Multiword role exclusions could lose their negation. | New app/intent.py recognizes adjacent role grammar and returns consumed token positions. Named-character/director/actor slots and exclusions remain typed. Unknown requested roles clarify. |
| planner context | An exact title always won even in an actor filmography-style request sharing that spelling. | Bare/play requests keep title intent; `NAME movies` with actor evidence selects actor intent. Explicit role cues override that default. |
| gazetteer fuzzy candidates | Only whole titles and partial person names were indexed for typo correction. A partial-title typo could succeed only through an incidental character correction. | Add title-word candidates to the existing trigram/skeleton indexes, retaining whole-word/context checks and ambiguity handling. |
| alias transliteration | Raw edit distance could not connect Talabadi to Thalapathy; their existing skeletons differ by two edits. | A bounded approximate-skeleton candidate path is enabled only for actor aliases in name/listing context. Requires long input, a clear canonical target and appropriate role; competing aliases clarify. No example-specific spelling rule was added. |
| numeric / Unicode titles | Numeric tokens were discarded as operators; the empty-query check counted only ASCII letters and title-fragment checks rejected combining marks. | Protect observed exact numeric titles, admit numeric title slots with structured language context, and count Unicode letters correctly. Explicit year requests remain year requests. |
| service title/browse path | Results bypassed FTS, BGE and RRF and sorted alphabetically. | Lead-cast relationship evidence precedes supporting appearances for actor listings; title fragments prefer earlier whole-word position, then fewer unmatched words. Explicit date sorting remains authoritative. Supporting appearances remain eligible. |
| service lexical relaxation | Fewer than five strict hits caused the strict list to be replaced by weaker-stage hits. A high-frequency one-word hit could replace an all-word hit. | Retain stronger-stage ordering, append newly relaxed matches, and deduplicate. Weighted RRF still combines lexical/dense channels. |
| service uncalibrated fallback | An unsupported short query with zero lexical/entity evidence returned arbitrary dense nearest neighbors. | In default hybrid search, uncalibrated semantic neighbors alone do not answer an unfiltered one-content-word lookup. Ask for more detail. Descriptive/mood discovery and diagnostic dense-only comparisons remain available. |

Two old tests asserted that `starring`/`featuring` a character-only name should
return the character's movie. Those expectations embodied the reported role bug;
they now require unknown-name clarification. Explicit character tests are retained.

## Why these approaches

Changing a universal title/cast/director weight hierarchy would not fix the main
failures: the wrong movies were already admitted by hard filters, and listing
queries bypassed scoring entirely. The importer already weights title A,
genres/themes B and body C; cast/director/character relationships are resolved
through metadata, not those FTS documents. No FTS weight, schema, importer or
embedding-text changes were needed.

PostgreSQL describes structural weights and ranking as application-dependent;
that supports keeping relationship correctness separate from text relevance.
[PostgreSQL 16 text-search controls](https://www.postgresql.org/docs/16/textsearch-controls.html).

Broader unrestricted fuzzy expansion would create false positives and additional
work. The implementation retains indexed, bounded candidate generation and checks
context and ambiguity before correction. This follows the cost/precision concerns
documented for edit-distance expansion, without adding Elasticsearch as a dependency.
[Elastic fuzzy-query documentation](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-fuzzy-query).

A fabricated global cosine threshold would be unjustified. The BGE model card
warns that high raw similarities do not themselves establish relatedness and that
thresholds depend on the target data. The short-query safeguard therefore uses
absence of evidence and insufficient query detail, rather than fitting a score to
these examples. [Official BGE model card](https://huggingface.co/BAAI/bge-small-en-v1.5).

## Actual before / after API results

Ranks below are observed API ranks on the fixed synthetic catalog. OP 2 is a
supporting Vijay appearance in this fixture and remains a valid result.

| Query | Before | After |
|---|---|---|
| Parasakthi | Parasakthi – Uncensored Version #1 | Same correct #1 |
| Parimila | Parimala and Co #1, reached through a character correction | Same #1, corrected directly to the title |
| Parimala and Co | Correct exact title #1 | Same |
| Vijay movies | Abhinethri #1 (character-only), Bhairava #2, OP 2 #3 | Bhairava #1, OP 2 #2; character-only movie excluded |
| Thalapathy movies | Competing movie titled Thalapathy #1 | Bhairava #1, OP 2 #2 through actor alias |
| Talabadi movies | Unconstrained retrieval; Thalapathy #1, Abhinethri #2; all 28 movies returned | Bhairava #1, OP 2 #2; visible transliteration correction to Vijay |
| Ilayathalapathy movies | Bhairava #1, OP 2 #2 | Same correct results |
| Parasakthi Uncensored Version | Correct title #1 | Same |
| high (High Tide and Highway both exist) | High Tide #1 | Same whole-word title result; Highway excluded |
| 2 | Empty-query clarification | Genuine title 2 #1 |
| Ka | Genuine short title Ka #1 | Same |
| Movies with a character named Vijay | Bhairava #1, OP 2 #2, Abhinethri #3 | Abhinethri only, #1; no residual character-cue retrieval |
| Movies directed by Shankar | Bhairava #1 | Same correct director result |
| Sallu Bhai films | Distant Shores (character-only) | Moonlit Letters (actual actor) |
| Dev Patel movies | Amber Station #1 (character-only), Copper Horizon #2 | Copper Horizon only, #1 |
| high / quuxnonsense (separate Highway-only control) | Highway recommended for both | No recommendations; asks for more detail |

Parasakthi, complete titles and several other cases already passed the current
baseline because of earlier fixes. This investigation does not claim to have
newly fixed their result order. Parimila's baseline success depended on the
incidental Parimala character; additional tests remove that dependency.

## Quantitative results and limits

| Measurement | Before | After |
|---|---:|---:|
| Fixed planner judgments | 43/63 | 63/63 |
| Same judgments through actual API + PG + BGE | 43/63 | 63/63 |
| Finite-set recommendation micro precision, 50 labelled queries | 0.5385 | 1.0000 |
| Finite-set recommendation micro recall | 0.9180 | 1.0000 |
| Mean reciprocal rank over those 50 queries | 0.7729 | 1.0000 |
| Incorrect inclusions across those queries | 48 | 0 |
| Missing relevant results across those queries | 5 | 0 |
| Highway-only no-evidence controls | 0/2 | 2/2 |

The fixed set has 20 improved judgments and zero regressions. These numbers apply
only to these constructed labels. They are **not catalog-wide accuracy**, a
semantic relevance benchmark, or a statistical estimate of production quality.
Safety checks test interpretation, not the relevance of every recommended plot.

Added 37 adversarial edge cases using other names, including typed collisions,
near-alias ambiguity, partial-title typos without character support, numeric and
Tamil titles, unknown roles and narrative negatives. All pass. Six failures found
while developing that suite were corrected; it is a regression suite, not a blind
held-out accuracy estimate. Added four typed-negation checks and seven short-query
evidence checks, alongside role grammar, ranking and lexical-tier tests.

Final verification: **210 Python tests**, including 15 guarded PostgreSQL cases
and real BGE import/reuse; **2 JavaScript UI tests**. The main 63-query evaluations
and two negative API controls are additional to pytest.

## Performance

| Scope | Before p50 / p95 / maximum | After p50 / p95 / maximum |
|---|---|---|
| 63 serial ASGI API requests on 28 fixture movies, real PG/BGE | 4.721 / 42.072 / 60.884 ms | 4.971 / 46.451 / 74.577 ms |
| 189 serial planner requests on 2,357 synthetic movies | 1.486 / 26.622 / 39.073 ms | 1.315 / 15.654 / 41.085 ms |

The API sample p95 increased about 4.4 ms; planner sample p95 decreased. These
single-run measurements are noisy and not a controlled throughput comparison.
The API set is 28 movies; the 2,357-record run measures the planner only. Neither
includes process/model startup or establishes Mac/concurrent-service latency.
No new model or external runtime dependency was introduced. Fuzzy verification is
bounded by indexed candidate windows; title-word lookup does not scan all movies.
Listing sorting still processes its eligible results as before. New index memory
was not separately measured.

## Remaining limitations and tradeoffs

- The actual Mac JSON, credits and failed live search traces were unavailable.
  Verify these changes on that export before claiming real-catalog improvement.
- Detailed semantic discovery remains uncalibrated. For example, the fixture
  query `a child is kidnapped` correctly ranks Family Secrets first but still
  returns unrelated dense neighbors afterward. That tail is **not fixed** by
  these entity/short-query changes. Mood relevance also lacks human labels.
- Short unsupported one-word semantic-only discovery now abstains. That reduces
  false recommendations but can reject a meaningful concept expressed in one
  word. Add plot detail or use a calibrated deployment threshold. Diagnostic
  dense-only evaluation intentionally keeps the original behavior.
- `NAME movies` with actor evidence is interpreted as filmography; bare/play
  requests favor titles. This is a contextual default, not certainty about every
  ambiguous user's intention. Explicit actor/character/title cues are supported.
- Approximate alias phonetics can still encounter ambiguity. It is confined to
  long alias strings in actor/listing contexts; competing canonical targets
  clarify. Raw short substrings and arbitrary prefix completion are not enabled.
- Actor records with identical names cannot be separated without stable person
  identifiers. Missing or inaccurate credits remain a data-quality limitation.
- Primary-cast listing order uses supplied credits; it is not a popularity or
  quality score. RRF still does not represent probability, and lexical tiers do
  not constitute semantic rejection calibration.

## Files, evidence and running on Mac

Application changes: app/intent.py, app/planner.py, app/gazetteer.py and
app/service.py. Query synonyms remain solely in app/vocab.py, unchanged here.
No importer, schema, vector or database-title edits are required. Restart the app
after copying/pulling these code changes; existing embeddings remain compatible.

Full per-query evidence is in evaluation/api-before.json and api-after.json;
planner/filter evidence is in evaluation/planner-before.json and planner-after.json.
They contain only synthetic fixtures/results. Baseline code was preserved under
ignored runtime/relevance/baseline; the baseline commit is available locally.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -m 'not integration'
python -m scripts.evaluate_relevance --output runtime/relevance/mac-planner.json
```

For integration/evaluation, use a **disposable** database whose name includes
`test`. The pytest fixtures reset schema_v2_large there; the evaluation importer
changes its active synthetic snapshot. Never use the populated working catalog.

```bash
export LARGE_TEST_DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/catalog_test'
export LARGE_TEST_REAL_MODEL=1
export MODELS_DIR='/parent/of/bge-small-en-v1.5'
python -m pytest
export LARGE_DATABASE_URL="$LARGE_TEST_DATABASE_URL"
python -m scripts.evaluate_catalog_api --ingest --negative-controls \
  --output runtime/relevance/mac-api.json
```

For manual validation on the real catalog, use the app normally with its existing
LARGE_DATABASE_URL and inspect debug interpretations/corrections. Do not run the
synthetic import/test commands against that database. A real-catalog development
and held-out label set is still needed for semantic-tail calibration and relevance
measurement; thresholds were not fitted to these synthetic examples.
