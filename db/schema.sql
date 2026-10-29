-- Text Search V2 schema. Fully isolated: V1 tables (public.cms, public.scene_embeddings)
-- are never read or modified. Requires only the pgvector extension.
-- Re-runnable: every statement is idempotent.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS search_v2;

-- One row per built index generation. Documents and embeddings belong to a generation;
-- the service reads only the ACTIVE one, so a rebuild can never be half-visible.
CREATE TABLE IF NOT EXISTS search_v2.generation (
    generation_id     BIGSERIAL PRIMARY KEY,
    status            TEXT NOT NULL DEFAULT 'building'
                      CHECK (status IN ('building', 'ready', 'retired', 'failed')),
    embedding_model   TEXT,
    model_revision    TEXT,
    query_prefix      TEXT NOT NULL DEFAULT '',
    pooling           TEXT,
    dim               INTEGER NOT NULL DEFAULT 384,
    text_version      TEXT NOT NULL,
    passage_tokens    INTEGER NOT NULL,
    overlap_sentences INTEGER NOT NULL DEFAULT 0,
    scene_header      BOOLEAN NOT NULL DEFAULT FALSE,
    doc_count         INTEGER NOT NULL DEFAULT 0,
    embedded_count    INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at      TIMESTAMPTZ,
    notes             JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Single-row pointer; switching is one UPDATE inside one transaction (atomic).
CREATE TABLE IF NOT EXISTS search_v2.active_generation (
    singleton              BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    generation_id          BIGINT NOT NULL REFERENCES search_v2.generation (generation_id),
    previous_generation_id BIGINT REFERENCES search_v2.generation (generation_id),
    switched_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Catalog record: one row per asset/language pair.
CREATE TABLE IF NOT EXISTS search_v2.asset (
    asset_id               TEXT NOT NULL,
    language               TEXT NOT NULL,
    asset_key              TEXT GENERATED ALWAYS AS (asset_id || '|' || language) STORED,
    title                  TEXT NOT NULL,
    title_norm             TEXT NOT NULL,
    description            TEXT,
    genres                 TEXT[] NOT NULL DEFAULT '{}',
    genres_norm            TEXT[] NOT NULL DEFAULT '{}',
    cast_names             TEXT[] NOT NULL DEFAULT '{}',
    cast_norm              TEXT[] NOT NULL DEFAULT '{}',
    characters             TEXT[] NOT NULL DEFAULT '{}',
    directors              TEXT[] NOT NULL DEFAULT '{}',
    content_type           TEXT,                 -- movie | show | original | NULL (unknown)
    original_release_year  INTEGER,              -- ONLY from a declared source field
    original_release_date  DATE,
    platform_release_date  DATE,                 -- platform arrival; never used as release year
    movie_gcs_path         TEXT NOT NULL CHECK (movie_gcs_path LIKE 'gs://%'),
    thumbnail_image_url    TEXT,
    total_scenes           INTEGER NOT NULL CHECK (total_scenes >= 0),
    cms_metadata           JSONB NOT NULL,
    source_metadata        JSONB NOT NULL,
    source_file            TEXT NOT NULL,
    source_sha256          TEXT NOT NULL,
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, language),
    CHECK (asset_id <> '' AND language <> '' AND title <> '')
);
CREATE UNIQUE INDEX IF NOT EXISTS asset_key_uidx ON search_v2.asset (asset_key);

-- Registered aliases (explicit config + derived variants such as the GCS folder slug).
CREATE TABLE IF NOT EXISTS search_v2.asset_alias (
    asset_id    TEXT NOT NULL,
    language    TEXT NOT NULL,
    alias       TEXT NOT NULL,
    alias_norm  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'registered',
    PRIMARY KEY (asset_id, language, alias_norm),
    FOREIGN KEY (asset_id, language) REFERENCES search_v2.asset (asset_id, language) ON DELETE CASCADE
);

-- Scene record: complete original scene retained in scene_data.
CREATE TABLE IF NOT EXISTS search_v2.scene (
    asset_id             TEXT NOT NULL,
    language             TEXT NOT NULL,
    asset_key            TEXT GENERATED ALWAYS AS (asset_id || '|' || language) STORED,
    scene_id             TEXT NOT NULL,
    scene_order          INTEGER NOT NULL CHECK (scene_order > 0),
    chunk_id             TEXT,
    start_time           TEXT,
    end_time             TEXT,
    start_seconds        DOUBLE PRECISION NOT NULL,
    end_seconds          DOUBLE PRECISION NOT NULL,
    description          TEXT,
    master_summary       TEXT,           -- cleaned (template prefix removed)
    visual_summary       TEXT,
    dialogue_transcript  TEXT,           -- original, unmodified
    important_dialogue   TEXT,
    dialogue_turns       JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{speaker, original, gloss}]
    mood                 TEXT,
    action_intensity     TEXT,
    barc_retention_score TEXT,
    engaging_score       TEXT,
    energy_level         TEXT,
    cast_names           TEXT[] NOT NULL DEFAULT '{}',
    cast_norm            TEXT[] NOT NULL DEFAULT '{}',
    characters           TEXT[] NOT NULL DEFAULT '{}',
    characters_norm      TEXT[] NOT NULL DEFAULT '{}',
    objects              TEXT[] NOT NULL DEFAULT '{}',
    tags                 TEXT[] NOT NULL DEFAULT '{}',
    scene_data           JSONB NOT NULL,
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, language, scene_id),
    FOREIGN KEY (asset_id, language) REFERENCES search_v2.asset (asset_id, language) ON DELETE CASCADE,
    CHECK (end_seconds > start_seconds)
);
CREATE INDEX IF NOT EXISTS scene_asset_order_idx ON search_v2.scene (asset_key, scene_order);
CREATE INDEX IF NOT EXISTS scene_cast_norm_idx ON search_v2.scene USING GIN (cast_norm);
CREATE INDEX IF NOT EXISTS scene_characters_norm_idx ON search_v2.scene USING GIN (characters_norm);

-- Immutable full snapshots. The legacy asset/scene tables above are an ACTIVE
-- projection for local diagnostics only; serving always pins these snapshots.
-- Existing pre-snapshot indexes must be reimported before serving/rollback.
CREATE TABLE IF NOT EXISTS search_v2.asset_snapshot (
    LIKE search_v2.asset INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING CONSTRAINTS,
    generation_id BIGINT NOT NULL REFERENCES search_v2.generation ON DELETE CASCADE,
    PRIMARY KEY (generation_id, asset_id, language)
);
CREATE UNIQUE INDEX IF NOT EXISTS asset_snapshot_key_idx
    ON search_v2.asset_snapshot (generation_id, asset_key);
CREATE TABLE IF NOT EXISTS search_v2.scene_snapshot (
    LIKE search_v2.scene INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING CONSTRAINTS,
    generation_id BIGINT NOT NULL REFERENCES search_v2.generation ON DELETE CASCADE,
    PRIMARY KEY (generation_id, asset_id, language, scene_id),
    FOREIGN KEY (generation_id, asset_id, language)
        REFERENCES search_v2.asset_snapshot (generation_id, asset_id, language) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS scene_snapshot_lookup_idx
    ON search_v2.scene_snapshot (generation_id, asset_key, scene_id);
CREATE TABLE IF NOT EXISTS search_v2.alias_snapshot (
    generation_id BIGINT NOT NULL,
    asset_id TEXT NOT NULL,
    language TEXT NOT NULL,
    alias TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'registered',
    PRIMARY KEY (generation_id, asset_id, language, alias_norm),
    FOREIGN KEY (generation_id, asset_id, language)
        REFERENCES search_v2.asset_snapshot (generation_id, asset_id, language) ON DELETE CASCADE
);

-- Derived search documents (one row per passage), scoped to a generation.
--   scope = catalog | scene ; kind = main | dialogue
--   tsv_main   : weighted 'english' vector (title A, genres B, ...); main documents
--   tsv_simple : 'simple' vector (no stop-word removal, no stemming); dialogue documents,
--                so phrase queries such as "I will return" keep every word.
CREATE TABLE IF NOT EXISTS search_v2.search_doc (
    doc_id        BIGSERIAL PRIMARY KEY,
    generation_id BIGINT NOT NULL REFERENCES search_v2.generation (generation_id) ON DELETE CASCADE,
    scope         TEXT NOT NULL CHECK (scope IN ('catalog', 'scene')),
    kind          TEXT NOT NULL CHECK (kind IN ('main', 'dialogue')),
    asset_id      TEXT NOT NULL,
    language      TEXT NOT NULL,
    asset_key     TEXT NOT NULL,
    scene_id      TEXT,
    passage_no    INTEGER NOT NULL DEFAULT 1,
    text          TEXT NOT NULL,                 -- exact text that was embedded
    text_hash     TEXT NOT NULL,                 -- sha256(model|text_version|text)
    token_count   INTEGER NOT NULL,
    tsv_main      TSVECTOR,
    tsv_simple    TSVECTOR,
    embedding     vector(384)
);
-- Keep the 1.5 KB vector INSIDE the row. pgvector declares it 'external' (out-of-line) and a row with the
-- vector, the text and two tsvectors is slightly over the ~2 KB TOAST threshold, so about half of all vectors
-- were being fetched from the TOAST table one row at a time during the exact scan (measured: a 20k-scene
-- scan took ~85 ms vs ~25 ms for rows that stayed inline). PLAIN makes the toaster move the text columns
-- out-of-line instead. Takes effect for newly written rows; re-ingest to apply it to an existing table.
ALTER TABLE search_v2.search_doc ALTER COLUMN embedding SET STORAGE PLAIN;

CREATE UNIQUE INDEX IF NOT EXISTS search_doc_uidx
    ON search_v2.search_doc (generation_id, scope, kind, asset_key, COALESCE(scene_id, ''), passage_no);
CREATE INDEX IF NOT EXISTS search_doc_lookup_idx
    ON search_v2.search_doc (generation_id, scope, asset_key);
CREATE INDEX IF NOT EXISTS search_doc_hash_idx ON search_v2.search_doc (text_hash);
CREATE INDEX IF NOT EXISTS search_doc_tsv_main_idx ON search_v2.search_doc USING GIN (tsv_main);
CREATE INDEX IF NOT EXISTS search_doc_tsv_simple_idx ON search_v2.search_doc USING GIN (tsv_simple);
-- Corpus vocabulary of a generation (word -> number of documents containing it). Used by the query
-- planner to (a) never "correct" a word that really occurs in the data and (b) order query terms by
-- rarity when relaxing a lexical query. Built by the importer; rebuilt with every generation.
CREATE TABLE IF NOT EXISTS search_v2.lexicon (
    generation_id BIGINT NOT NULL REFERENCES search_v2.generation (generation_id) ON DELETE CASCADE,
    word          TEXT NOT NULL,
    df            INTEGER NOT NULL,
    PRIMARY KEY (generation_id, word)
);

-- No ANN (HNSW/IVFFlat) index on purpose: exact search at the current scale.
-- Add one only after profiling shows dense p95 above ~50-100 ms (see README).
