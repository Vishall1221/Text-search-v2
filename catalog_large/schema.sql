-- Six catalog-only tables. No changes to public or search_v2.
-- pgvector must already be enabled in the selected database.
CREATE SCHEMA IF NOT EXISTS schema_v2_large;

CREATE TABLE IF NOT EXISTS schema_v2_large.generation (
    generation_id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('building','ready','failed')),
    embedding_model TEXT,
    model_revision TEXT,
    pooling TEXT,
    query_prefix TEXT,
    dim INTEGER NOT NULL DEFAULT 384 CHECK (dim=384),
    text_version TEXT NOT NULL,
    passage_tokens INTEGER NOT NULL,
    asset_count INTEGER NOT NULL DEFAULT 0,
    doc_count INTEGER NOT NULL DEFAULT 0,
    notes JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS schema_v2_large.active_generation (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    generation_id BIGINT NOT NULL REFERENCES schema_v2_large.generation,
    previous_generation_id BIGINT REFERENCES schema_v2_large.generation,
    switched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS schema_v2_large.asset_snapshot (
    generation_id BIGINT NOT NULL REFERENCES schema_v2_large.generation ON DELETE CASCADE,
    show_id TEXT NOT NULL CHECK (show_id<>''),
    title TEXT NOT NULL CHECK (title<>''),
    title_norm TEXT NOT NULL,
    content_type TEXT NOT NULL,
    plot_summary TEXT NOT NULL DEFAULT '',
    themes TEXT[] NOT NULL DEFAULT '{}',
    genres TEXT[] NOT NULL DEFAULT '{}',
    genres_norm TEXT[] NOT NULL DEFAULT '{}',
    primary_language TEXT CHECK (primary_language<>''),
    audio_languages TEXT[] NOT NULL DEFAULT '{}',
    primary_cast JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(primary_cast)='array'),
    supporting_cast JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(supporting_cast)='array'),
    cast_names TEXT[] NOT NULL DEFAULT '{}',
    cast_norm TEXT[] NOT NULL DEFAULT '{}',
    character_names TEXT[] NOT NULL DEFAULT '{}',
    character_norm TEXT[] NOT NULL DEFAULT '{}',
    directors TEXT[] NOT NULL DEFAULT '{}',
    directors_norm TEXT[] NOT NULL DEFAULT '{}',
    source_released_date DATE,
    release_date DATE, -- Date used for this catalog's relative-date queries (ZEE5 by default).
    original_release_date DATE,
    platform_release_date DATE,
    source_metadata JSONB NOT NULL,
    source_file TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (generation_id,show_id)
);
-- Upgrade tables created by the initial importer. Unknown is NULL, never an audio-track guess.
ALTER TABLE schema_v2_large.asset_snapshot ALTER COLUMN primary_language DROP NOT NULL;

-- Nicknames refer ONLY to actors/actresses found in this generation's cast.
-- No movie aliases and no substring matching of canonical actor names.
CREATE TABLE IF NOT EXISTS schema_v2_large.alias_snapshot (
    generation_id BIGINT NOT NULL REFERENCES schema_v2_large.generation ON DELETE CASCADE,
    actor_name TEXT NOT NULL,
    actor_norm TEXT NOT NULL,
    alias TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    entity_kind TEXT NOT NULL DEFAULT 'actor' CHECK (entity_kind='actor'),
    requires_context BOOLEAN NOT NULL DEFAULT FALSE,
    provenance TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (generation_id,actor_norm,alias_norm)
);
CREATE TABLE IF NOT EXISTS schema_v2_large.search_doc (
    doc_id BIGSERIAL PRIMARY KEY,
    generation_id BIGINT NOT NULL,
    show_id TEXT NOT NULL,
    passage_no INTEGER NOT NULL CHECK (passage_no>0),
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    tsv_main TSVECTOR NOT NULL,
    embedding VECTOR(384),
    FOREIGN KEY (generation_id,show_id)
        REFERENCES schema_v2_large.asset_snapshot ON DELETE CASCADE,
    UNIQUE (generation_id,show_id,passage_no)
);
ALTER TABLE schema_v2_large.search_doc ALTER COLUMN embedding SET STORAGE PLAIN;
CREATE INDEX IF NOT EXISTS large_doc_lex_idx ON schema_v2_large.search_doc USING GIN(tsv_main);
CREATE INDEX IF NOT EXISTS large_doc_hash_idx ON schema_v2_large.search_doc(text_hash);
CREATE INDEX IF NOT EXISTS large_actor_idx ON schema_v2_large.asset_snapshot USING GIN(cast_norm);
CREATE INDEX IF NOT EXISTS large_audio_idx ON schema_v2_large.asset_snapshot USING GIN(audio_languages);
CREATE INDEX IF NOT EXISTS large_language_idx ON schema_v2_large.asset_snapshot(generation_id,primary_language);
CREATE TABLE IF NOT EXISTS schema_v2_large.lexicon (
    generation_id BIGINT NOT NULL REFERENCES schema_v2_large.generation ON DELETE CASCADE,
    word TEXT NOT NULL,
    df INTEGER NOT NULL CHECK (df>0),
    PRIMARY KEY(generation_id,word)
);
