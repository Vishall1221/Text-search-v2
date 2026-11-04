# V2 Large integration verification — 4 November 2026

This separate folder adapts Text-Search-v2 algorithms to the already-defined
`schema_v2_large` catalog-only tables. Original V1/V2 source and application
database tables were not changed. The tested source is published in the
`text-search-v2-large/` subfolder of the existing Text-search-v2 repository.

## Environment and scope

Windows, Python 3.11.9, PostgreSQL 16.15, pgvector 0.8.6, real local
BGE-small-en-v1.5 ONNX CPU encoder (384 dimensions). Tests used the existing
review Python environment and an isolated database named
`text_search_v2_large_integration_test`, cluster port 55439. The actual application
database at port 5432 was not started or modified. The temporary HTTP API used
port 18092; deployment defaults to 8002.

The actual 2,357-movie export exists only on the user's Mac. This report uses
synthetic fixtures. It is not a benchmark of the real films or Mac hardware.

## Correctness checks

**93 Python tests passed**, including 13 real-PostgreSQL cases and the real BGE
import/reuse test. **2 JavaScript UI tests passed** for late search responses and
safe text insertion. Inherited tests cover normalization, dates/operators, RRF
and passage/token budgets; new cases cover catalog-only planning and integration.

- Loaded metadata, actor aliases, vocabulary and active generation consistently.
- Exact titles, nicknames and misspellings; alias ambiguity and generic nickname
  context guards; canonical titles take precedence; no movie aliases imported.
- Multiple audio tracks per movie; explicit AND/OR membership and negated actors.
- No primary-language field in schema/results. Missing directors and character
  names stay unknown. Scene/dialogue/original-language/suitability requests clarify.
- Dynamic theme facets; existing and expanded genres; soft feel-good/heartwarming
  preferences; synonym OR alternatives; conservative non-name typo correction
  preserving the original semantic text; narrative regression cases.
- ZEE5 dates, explicit sorting, similar-title seed exclusion, lexical negatives.
- Generation switch and rollback reload matching aliases/metadata. Runtime-only
  searches use only the large schema; original namespaces were sentinel-checked.
- Model mismatch labels dense unavailable while lexical still works. Stale cutoff
  identity does not filter another corpus. No old demo calibration was copied.
- Deadlines, request-child cancellation, inference admission after cancellation,
  real SQL statement timeout/cancellation, and pool usability afterward.
- API readiness, metadata-only response cards, invalid-input 422s, UI static serving,
  and missing playback endpoint (404).

## Synthetic scale and BGE

Built 2,357 synthetic movie records, 2,357 document vectors and 22 active actor
aliases. First BGE build took approximately 49.95 seconds in this run. Reimport
reused all 2,357 vectors, embedded zero new texts and took 3.94 seconds. Max final
document length was 36 tokens; longer-passage splitting is covered separately.

Snapshot allocations measured with Python tracemalloc: about **26.08 MB current /
26.14 MB peak**. This excludes native ONNX allocations, model buffers, PostgreSQL
and other process memory. Snapshot loading under tracing took 3.58 seconds, which
is not a measurement of normal untraced startup latency.

The first load probe exposed inherited linear scans of partial actor-name tokens.
The fix uses trigram/skeleton candidate indexes with a bounded partial-name edit
comparison, excludes numeric/generic role tokens from partial names, preindexes
audio/type membership and caches immutable snapshot coverage. With the same
synthetic corpus, an earlier 8-client warm run had 41 degraded requests; the final
run below had zero. This is an optimization measured on these fixtures, not an
unlimited catalog-size guarantee.

Warm service requests (200 per concurrency, 15 repeated query forms):

| Clients | p50 ms | p95 ms | Max ms | Degraded |
|---|---:|---:|---:|---:|
| 1 | 3.56 | 42.24 | 55.63 | 0 |
| 4 | 9.19 | 113.60 | 173.11 | 0 |
| 8 | 17.53 | 149.79 | 203.72 | 0 |

Four labelled fixture top-1 checks passed: exact title, Thalapathy, Sallu Bhai and
child-abduction lexical synonym retrieval. These few constructed labels are not
a relevance study or a statistically established semantic-over-lexical gain.

## Actual HTTP smoke/load

Native Windows `python -m app.start` started the real HTTP service against the
2,357-record synthetic generation. Readiness reported dense=true, correct BGE,
schema_v2_large and expected counts. It correctly reported uncalibrated dense.

200 warm HTTP requests, 4 clients: **p50 29.1 ms, p95 133.1 ms, max 170.2 ms**;
zero HTTP errors and zero degraded responses. The warm p95 <1,000 ms gate passed.
The first distinct-query phase is not evidence of process-cold latency. The owned
test API and isolated review cluster were stopped after verification.

## Remaining validation

Run on the actual Mac export, check human-labelled query families and negatives,
verify ambiguity/mood preferences against real metadata, calibrate rejection on
that corpus, and measure Mac memory and concurrency. No actual reranker-model
benchmark, real-catalog accuracy claim or multilingual-query claim is made.

Runtime evidence is in ignored `runtime/verification/`; source/tests/docs are
included in the transfer package, while model weights and synthetic bulk data are
excluded. API startup is read-only and needs no data rebuild when the existing
generation/model match.

## Single-source vocabulary cleanup

Genre definitions now live only in config/query_synonyms.json alongside mood
preferences and lexical alternatives. app.synonyms loads the registry and
app.vocab re-exports the generated mapping. The legacy fiction group was moved
into the JSON to preserve behavior. All 81 database-independent tests passed,
including fiction/science-fiction separation. DB/retrieval code did not change.
Restart the app after pulling; no reimport is needed.
