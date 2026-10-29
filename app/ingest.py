"""Importer: validates source JSON, loads catalog/scene rows and builds a new index generation.

    python -m app.ingest --data ../Datas/output                # build + activate
    python -m app.ingest --data ... --dry-run                  # validate and report only
    python -m app.ingest --data ... --no-activate              # build, keep the old generation live
    python -m app.ingest --activate 7                          # switch to an existing ready generation
    python -m app.ingest --rollback                            # return to the previous generation

Properties required by the V2 plan:
* every source file is validated first; a bad file never produces a partial import;
* documents/embeddings belong to a *generation*; the service reads only the active one, and the
  pointer switches in one transaction (atomic), keeping the previous generation for rollback;
* embeddings are reused when ``text_hash`` (model + text version + text) is unchanged;
* nothing outside the ``search_v2`` schema is touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from app.documents import (_listify, build_catalog_passages, build_scene_passages, clean_master_summary,
                           text_hash, useful_tags)
from app.embedding import Embedder, load_embedder, vector_literal
from app.text import normalize
from config.settings import ROOT_DIR, settings

log = logging.getLogger("ingest")
SCHEMA_FILE = ROOT_DIR / "db" / "schema.sql"
KEEP_GENERATIONS = 2           # active + previous (rollback); older ones are deleted


class IngestError(Exception):
    pass


# ---------------------------------------------------------------------------------------
# Source loading and validation
# ---------------------------------------------------------------------------------------

def _get_path(obj: Any, dotted: str | None) -> Any:
    if not dotted:
        return None
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    if re.fullmatch(r"\d{4}", s):
        return date(int(s), 1, 1)
    return None


def load_profile(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or settings.import_profile).read_text(encoding="utf-8"))


def load_aliases(path: Path | None = None) -> dict[str, list[str]]:
    try:
        raw = json.loads((path or settings.aliases_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: [str(a) for a in v] for k, v in raw.items() if not k.startswith("_") and isinstance(v, list)}


def _cast_pairs(cms: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    info = cms.get("cast_info")
    if isinstance(info, list):
        for item in info:
            if isinstance(item, (list, tuple)) and item and isinstance(item[0], str) and item[0].strip():
                pairs.append((item[0].strip(), (item[1].strip() if len(item) > 1 and isinstance(item[1], str) else "")))
            elif isinstance(item, str) and item.strip():
                pairs.append((item.strip(), ""))
    if not pairs and isinstance(cms.get("cast_raw"), str):
        for part in cms["cast_raw"].split(","):
            name, _, char = part.partition(":")
            if name.strip():
                pairs.append((name.strip(), char.strip()))
    return list(dict.fromkeys(pairs))


def content_type_from_path(gcs_path: str, profile: dict[str, Any], raw: dict[str, Any] | None = None) -> str | None:
    """movie | show | original, from the declared source: a GCS path segment, or a named field."""
    cfg = profile.get("content_type") or {}
    if cfg.get("source") == "gcs_path_segment":
        segs = {k.upper(): v for k, v in (cfg.get("segments") or {}).items()}
        for part in gcs_path.split("/"):
            if part.upper() in segs:
                return segs[part.upper()]
        return None
    value = _get_path(raw or {}, cfg.get("field"))
    return str(value).strip().casefold() if value else None


def normalize_asset(raw: dict[str, Any], source_file: str, sha: str, profile: dict[str, Any],
                    aliases: dict[str, list[str]]) -> dict[str, Any]:
    cms = raw.get("cms_metadata") if isinstance(raw.get("cms_metadata"), dict) else {}
    language = str(raw.get("primary_language") or cms.get("primary_language") or "").strip().upper()
    title = str(raw.get("title") or cms.get("title") or "").strip()
    pairs = _cast_pairs(cms)
    genres = [g.strip() for g in (cms.get("genre") or []) if isinstance(g, str) and g.strip()]
    gcs = str(raw.get("movie_gcs_path") or "").strip()

    def declared(key: str) -> Any:
        field = (profile.get(key) or {}).get("field")
        return _get_path(raw, field) if field else None

    orig_date = _parse_date(declared("original_release_date"))
    orig_year_raw = declared("original_release_year")
    orig_year = int(orig_year_raw) if str(orig_year_raw or "").isdigit() else (orig_date.year if orig_date else None)
    directors = declared("directors")
    directors = [d.strip() for d in directors if isinstance(d, str) and d.strip()] if isinstance(directors, list) else []

    alias_set: dict[str, str] = {}
    for a in aliases.get(title, []):
        alias_set[normalize(a)] = "registered"
    m = re.search(r"/([^/]+)/[^/]*$", gcs)             # GCS folder slug: RAKTHA_VAJRAM -> raktha vajram
    if m:
        slug = normalize(m.group(1).replace("_", " "))
        if slug and slug != normalize(title):
            alias_set.setdefault(slug, "gcs_slug")
    alias_set.pop(normalize(title), None)

    return {
        "asset_id": str(raw.get("asset_id") or cms.get("asset_id") or "").strip(),
        "language": language,
        "title": title,
        "title_norm": normalize(title),
        "description": (cms.get("description") or "").strip() or None,
        "genres": genres,
        "genres_norm": [normalize(g) for g in genres],
        "cast_names": [n for n, _ in pairs],
        "cast_norm": [normalize(n) for n, _ in pairs],
        "characters": list(dict.fromkeys(c for _, c in pairs if c)),
        "directors": directors,
        "content_type": content_type_from_path(gcs, profile, raw),
        "original_release_year": orig_year,
        "original_release_date": orig_date,
        "platform_release_date": _parse_date(_get_path(raw, (profile.get("platform_release_date") or {}).get("field"))),
        "movie_gcs_path": gcs,
        "thumbnail_image_url": cms.get("thumbnail_image_url") or cms.get("asset_image"),
        "total_scenes": len(raw.get("scenes") or []),
        "cms_metadata": cms,
        "source_metadata": {k: v for k, v in raw.items() if k not in ("scenes", "cms_metadata")},
        "source_file": source_file,
        "source_sha256": sha,
        "aliases": alias_set,
    }


def validate_source(raw: Any, name: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(raw, dict):
        return [f"{name}: top level must be an object"]
    for key in ("asset_id", "title", "movie_gcs_path", "scenes"):
        if not raw.get(key):
            errs.append(f"{name}: missing required field '{key}'")
    if not (raw.get("primary_language") or (raw.get("cms_metadata") or {}).get("primary_language")):
        errs.append(f"{name}: missing primary_language")
    if isinstance(raw.get("movie_gcs_path"), str) and not raw["movie_gcs_path"].startswith("gs://"):
        errs.append(f"{name}: movie_gcs_path must start with gs://")
    scenes = raw.get("scenes")
    if not isinstance(scenes, list):
        return errs + [f"{name}: 'scenes' must be a list"]
    seen: set[str] = set()
    for i, sc in enumerate(scenes, start=1):
        tag = f"{name} scene #{i}"
        if not isinstance(sc, dict):
            errs.append(f"{tag}: not an object")
            continue
        sid = sc.get("scene_id")
        if not sid:
            errs.append(f"{tag}: missing scene_id")
        elif sid in seen:
            errs.append(f"{tag}: duplicate scene_id {sid}")
        seen.add(str(sid))
        try:
            s, e = float(sc["start_seconds"]), float(sc["end_seconds"])
            if not e > s:
                errs.append(f"{tag} ({sid}): end_seconds must be greater than start_seconds")
        except (KeyError, TypeError, ValueError):
            errs.append(f"{tag} ({sid}): start_seconds/end_seconds missing or not numeric")
        if not any(str(sc.get(k) or "").strip() for k in ("description", "master_scene_summary", "visual_summary")):
            errs.append(f"{tag} ({sid}): no description, summary or visual summary")
    return errs


def read_sources(data: Path) -> list[tuple[dict[str, Any], str, str]]:
    files = sorted(data.glob("*.json")) if data.is_dir() else [data]
    if not files:
        raise IngestError(f"No .json files found in {data}")
    out, errors = [], []
    for f in files:
        blob = f.read_bytes()
        try:
            raw = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{f.name}: unreadable JSON ({exc})")
            continue
        errors.extend(validate_source(raw, f.name))
        out.append((raw, f.name, hashlib.sha256(blob).hexdigest()))
    keys: dict[tuple[str, str], str] = {}
    for raw, name, _ in out:
        if isinstance(raw, dict):
            lang = str(raw.get("primary_language") or (raw.get("cms_metadata") or {}).get("primary_language") or "").upper()
            k = (str(raw.get("asset_id")), lang)
            if k in keys:
                errors.append(f"{name}: duplicate asset {k[0]} / {k[1]} (also in {keys[k]})")
            keys[k] = name
    if errors:
        raise IngestError("Validation failed:\n  " + "\n  ".join(errors))
    return out


# ---------------------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------------------

def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


def upsert_catalog(conn: psycopg.Connection, items: list[tuple[dict[str, Any], dict[str, Any]]], gen: int) -> None:
    with conn.cursor() as cur:
        for asset, raw in items:
            cur.execute(
                """INSERT INTO search_v2.asset_snapshot
                   (generation_id, asset_id, language, title, title_norm, description, genres, genres_norm, cast_names, cast_norm,
                    characters, directors, content_type, original_release_year, original_release_date,
                    platform_release_date, movie_gcs_path, thumbnail_image_url, total_scenes, cms_metadata,
                    source_metadata, source_file, source_sha256)
                   VALUES (%(gen)s,%(asset_id)s,%(language)s,%(title)s,%(title_norm)s,%(description)s,%(genres)s,%(genres_norm)s,
                           %(cast_names)s,%(cast_norm)s,%(characters)s,%(directors)s,%(content_type)s,
                           %(original_release_year)s,%(original_release_date)s,%(platform_release_date)s,
                           %(movie_gcs_path)s,%(thumbnail_image_url)s,%(total_scenes)s,%(cms)s,%(src)s,
                           %(source_file)s,%(source_sha256)s)
                   ON CONFLICT (generation_id, asset_id, language) DO UPDATE SET
                     title=EXCLUDED.title, title_norm=EXCLUDED.title_norm, description=EXCLUDED.description,
                     genres=EXCLUDED.genres, genres_norm=EXCLUDED.genres_norm, cast_names=EXCLUDED.cast_names,
                     cast_norm=EXCLUDED.cast_norm, characters=EXCLUDED.characters, directors=EXCLUDED.directors,
                     content_type=EXCLUDED.content_type, original_release_year=EXCLUDED.original_release_year,
                     original_release_date=EXCLUDED.original_release_date,
                     platform_release_date=EXCLUDED.platform_release_date, movie_gcs_path=EXCLUDED.movie_gcs_path,
                     thumbnail_image_url=EXCLUDED.thumbnail_image_url, total_scenes=EXCLUDED.total_scenes,
                     cms_metadata=EXCLUDED.cms_metadata, source_metadata=EXCLUDED.source_metadata,
                     source_file=EXCLUDED.source_file, source_sha256=EXCLUDED.source_sha256, ingested_at=now()""",
                {**asset, "gen": gen, "cms": Jsonb(asset["cms_metadata"]), "src": Jsonb(asset["source_metadata"])})
            cur.execute("DELETE FROM search_v2.alias_snapshot WHERE generation_id=%s AND asset_id=%s AND language=%s",
                        (gen, asset["asset_id"], asset["language"]))
            for alias_norm, kind in asset["aliases"].items():
                cur.execute("INSERT INTO search_v2.alias_snapshot (generation_id, asset_id, language, alias, alias_norm, kind) "
                            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (gen, asset["asset_id"], asset["language"], alias_norm, alias_norm, kind))


def upsert_scene(cur: psycopg.Cursor, asset: dict[str, Any], scene: dict[str, Any], order: int,
                 turns: list[Any], tags: list[str], gen: int) -> None:
    chars = _listify(scene.get("entities_characters"))
    cast = _listify(scene.get("cast"))
    cur.execute(
        """INSERT INTO search_v2.scene_snapshot
           (generation_id, asset_id, language, scene_id, scene_order, chunk_id, start_time, end_time, start_seconds, end_seconds,
            description, master_summary, visual_summary, dialogue_transcript, important_dialogue, dialogue_turns,
            mood, action_intensity, barc_retention_score, engaging_score, energy_level, cast_names, cast_norm,
            characters, characters_norm, objects, tags, scene_data)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (generation_id, asset_id, language, scene_id) DO UPDATE SET
             scene_order=EXCLUDED.scene_order, chunk_id=EXCLUDED.chunk_id, start_time=EXCLUDED.start_time,
             end_time=EXCLUDED.end_time, start_seconds=EXCLUDED.start_seconds, end_seconds=EXCLUDED.end_seconds,
             description=EXCLUDED.description, master_summary=EXCLUDED.master_summary,
             visual_summary=EXCLUDED.visual_summary, dialogue_transcript=EXCLUDED.dialogue_transcript,
             important_dialogue=EXCLUDED.important_dialogue, dialogue_turns=EXCLUDED.dialogue_turns,
             mood=EXCLUDED.mood, action_intensity=EXCLUDED.action_intensity,
             barc_retention_score=EXCLUDED.barc_retention_score, engaging_score=EXCLUDED.engaging_score,
             energy_level=EXCLUDED.energy_level, cast_names=EXCLUDED.cast_names, cast_norm=EXCLUDED.cast_norm,
             characters=EXCLUDED.characters, characters_norm=EXCLUDED.characters_norm, objects=EXCLUDED.objects,
             tags=EXCLUDED.tags, scene_data=EXCLUDED.scene_data, ingested_at=now()""",
        (gen, asset["asset_id"], asset["language"], str(scene["scene_id"]), order, scene.get("chunk_id"),
         scene.get("start_time"), scene.get("end_time"), float(scene["start_seconds"]), float(scene["end_seconds"]),
         scene.get("description"), clean_master_summary(scene.get("master_scene_summary"), scene.get("description")),
         scene.get("visual_summary"), scene.get("dialogue_transcript"), scene.get("important_dialogue"),
         Jsonb([t.as_dict() for t in turns]), scene.get("mood"), scene.get("action_intensity"),
         scene.get("barc_retention_score"), scene.get("engaging_score"), scene.get("energy_level"),
         cast, [normalize(c) for c in cast], chars, [normalize(c) for c in chars],
         _listify(scene.get("entities_objects")), tags, Jsonb(scene)))


_INSERT_DOC = """
INSERT INTO search_v2.search_doc
 (generation_id, scope, kind, asset_id, language, asset_key, scene_id, passage_no, text, text_hash, token_count,
  tsv_main, tsv_simple, embedding)
VALUES (%(gen)s,%(scope)s,%(kind)s,%(asset_id)s,%(language)s,%(asset_key)s,%(scene_id)s,%(passage_no)s,%(text)s,
        %(hash)s,%(tokens)s,
        CASE WHEN %(has_main)s THEN
             setweight(to_tsvector('english', %(A)s::text),'A') || setweight(to_tsvector('english', %(B)s::text),'B') ||
             setweight(to_tsvector('english', %(C)s::text),'C') || setweight(to_tsvector('english', %(D)s::text),'D') END,
        CASE WHEN %(simple)s::text IS NOT NULL THEN to_tsvector('simple', %(simple)s::text) END,
        %(emb)s::vector)
"""


def _parse_vec(s: str) -> np.ndarray:
    return np.asarray([float(x) for x in s.strip("[]").split(",")], dtype=np.float32)


def existing_embeddings(conn: psycopg.Connection, hashes: list[str]) -> dict[str, np.ndarray]:
    found: dict[str, np.ndarray] = {}
    with conn.cursor() as cur:
        for i in range(0, len(hashes), 500):
            cur.execute("SELECT DISTINCT ON (text_hash) text_hash, embedding::text FROM search_v2.search_doc "
                        "WHERE text_hash = ANY(%s) AND embedding IS NOT NULL", (hashes[i:i + 500],))
            for h, emb in cur.fetchall():
                found[h] = _parse_vec(emb)
    return found


# ---------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------

def build_passages(sources: list[tuple[dict[str, Any], str, str]], embedder: Embedder, profile: dict[str, Any],
                   aliases: dict[str, list[str]], max_tokens: int):
    """Pure transformation (no database): returns (assets, scene_rows, passages)."""
    assets, scene_rows, passages = [], [], []
    count = embedder.count_tokens
    for raw, name, sha in sources:
        asset = normalize_asset(raw, name, sha, profile, aliases)
        assets.append((asset, raw))
        passages.extend(build_catalog_passages(asset, count, max_tokens, settings.passage_overlap_sentences))
        ordered = sorted(enumerate(raw["scenes"]), key=lambda p: (float(p[1]["start_seconds"]), p[0]))
        for order, (_, scene) in enumerate(ordered, start=1):
            ps, turns = build_scene_passages(asset, scene, count, max_tokens, settings.passage_overlap_sentences,
                                             settings.scene_context_header)
            scene_rows.append((asset, scene, order, turns, useful_tags(scene, asset["title"], asset["language"])))
            passages.extend(ps)
    return assets, scene_rows, passages


def model_id(embedder: Embedder) -> str:
    p = embedder.profile
    return f"{p.name}:{embedder.revision}:{p.pooling}"


def ingest(data: Path, embedder: Embedder, *, dry_run: bool = False, activate: bool = True,
           database_url: str | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    profile, aliases = load_profile(), load_aliases()
    sources = read_sources(data)
    # count_tokens counts the complete encoded input, including special tokens.
    max_tokens = min(settings.passage_tokens, embedder.profile.max_len, 512)
    try:
        assets, scene_rows, passages = build_passages(sources, embedder, profile, aliases, max_tokens)
    except ValueError as exc:
        raise IngestError(f"Cannot construct model-safe passages: {exc}") from exc
    mid = model_id(embedder)
    report: dict[str, Any] = {
        "corpus_sha256": hashlib.sha256(json.dumps(sorted((name, sha) for _, name, sha in sources)).encode()).hexdigest(),
        "assets": len(assets), "scenes": len(scene_rows), "documents": len(passages),
        "catalog_docs": sum(p.scope == "catalog" for p in passages),
        "scene_main_docs": sum(p.scope == "scene" and p.kind == "main" for p in passages),
        "dialogue_docs": sum(p.kind == "dialogue" for p in passages),
        "split_passages": sum(p.passage_no > 1 for p in passages),
        "max_passage_tokens": max((p.token_count for p in passages), default=0),
        "passage_token_limit": max_tokens, "model": mid,
        "content_types": sorted({a["content_type"] or "unknown" for a, _ in assets}),
    }
    if dry_run:
        report["seconds"] = round(time.perf_counter() - t0, 2)
        return report

    with psycopg.connect(database_url or settings.database_url) as conn:
        apply_schema(conn)
        # -- new generation ------------------------------------------------------------
        gen = conn.execute(
            """INSERT INTO search_v2.generation (embedding_model, model_revision, query_prefix, pooling, dim,
                                                  text_version, passage_tokens, overlap_sentences, scene_header)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING generation_id""",
            (embedder.profile.name, embedder.revision, embedder.profile.query_prefix, embedder.profile.pooling,
             embedder.profile.dim, settings.text_version, max_tokens, settings.passage_overlap_sentences,
             settings.scene_context_header)).fetchone()[0]
        conn.commit()
        try:
            upsert_catalog(conn, assets, gen)
            with conn.cursor() as cur:
                for asset, scene, order, turns, tags in scene_rows:
                    upsert_scene(cur, asset, scene, order, turns, tags, gen)
            hashes = [text_hash(mid, settings.text_version, p) for p in passages]
            reuse = existing_embeddings(conn, list(set(hashes)))
            todo = [i for i, h in enumerate(hashes) if h not in reuse]
            t_embed = time.perf_counter()
            fresh: dict[str, np.ndarray] = {}
            uniq: dict[str, int] = {}
            for i in todo:
                uniq.setdefault(hashes[i], i)
            order = list(uniq.items())
            for s in range(0, len(order), 64):
                batch = order[s:s + 64]
                vecs = embedder.encode_docs([passages[i].text for _, i in batch])
                for (h, _), v in zip(batch, vecs):
                    fresh[h] = v
                log.info("embedded %d/%d", min(s + 64, len(order)), len(order))
            report["embedded"] = len(fresh)
            report["reused_embeddings"] = len(set(hashes)) - len(fresh)
            report["embed_seconds"] = round(time.perf_counter() - t_embed, 2)

            rows = []
            for p, h in zip(passages, hashes):
                vec = reuse.get(h)
                if vec is None:
                    vec = fresh[h]
                w = p.lex_weights
                rows.append({"gen": gen, "scope": p.scope, "kind": p.kind, "asset_id": p.asset_id, "language": p.language,
                             "asset_key": p.asset_key, "scene_id": p.scene_id, "passage_no": p.passage_no, "text": p.text,
                             "hash": h, "tokens": p.token_count, "has_main": bool(w),
                             "A": w.get("A", ""), "B": w.get("B", ""), "C": w.get("C", ""), "D": w.get("D", ""),
                             "simple": p.lex_simple, "emb": vector_literal(vec)})
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(_INSERT_DOC, rows)
                cur.execute("UPDATE search_v2.generation SET doc_count=%s, embedded_count=%s WHERE generation_id=%s",
                            (len(rows), len(rows), gen))
            with conn.transaction(), conn.cursor() as cur:
                vocab = build_lexicon(passages)
                with cur.copy("COPY search_v2.lexicon (generation_id, word, df) FROM STDIN") as cp:
                    for word, df in vocab.items():
                        cp.write_row((gen, word, df))
            report["lexicon_words"] = len(vocab)
            _verify_generation(conn, gen, expected=len(rows), assets=len(assets), scenes=len(scene_rows))
            conn.execute("UPDATE search_v2.generation SET status='ready', notes=%s WHERE generation_id=%s",
                         (Jsonb(report), gen))
            conn.commit()
        except Exception:
            conn.rollback()
            conn.execute("UPDATE search_v2.generation SET status='failed' WHERE generation_id=%s", (gen,))
            conn.commit()
            raise
        report["generation_id"] = gen
        if activate:
            switch_generation(conn, gen)
            report["activated"] = True
        prune_generations(conn)
    report["seconds"] = round(time.perf_counter() - t0, 2)
    return report


_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def build_lexicon(passages) -> dict[str, int]:
    """word -> number of documents containing it (folded, whole words, length >= 2)."""
    from collections import Counter
    df: Counter[str] = Counter()
    for p in passages:
        # Only text that is actually indexed lexically (not the label words added for the embedder).
        blob = " ".join([*p.lex_weights.values(), p.lex_simple or ""])
        df.update({w for w in _WORD.findall(blob) if len(w) >= 2})
    return dict(df)


def _verify_generation(conn: psycopg.Connection, gen: int, *, expected: int, assets: int, scenes: int) -> None:
    n, nulls, cat, sc = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE embedding IS NULL),
                  count(DISTINCT asset_key) FILTER (WHERE scope='catalog'),
                  count(DISTINCT (asset_key, scene_id)) FILTER (WHERE scope='scene' AND kind='main')
           FROM search_v2.search_doc WHERE generation_id=%s""", (gen,)).fetchone()
    problems = []
    if n != expected:
        problems.append(f"document count {n} != expected {expected}")
    if nulls:
        problems.append(f"{nulls} documents without an embedding")
    if cat != assets:
        problems.append(f"catalog documents for {cat} assets, expected {assets}")
    if sc != scenes:
        problems.append(f"scene documents for {sc} scenes, expected {scenes}")
    if problems:
        raise IngestError("Generation verification failed: " + "; ".join(problems))


def switch_generation(conn: psycopg.Connection, gen: int) -> None:
    with conn.transaction():
        row = conn.execute("SELECT status FROM search_v2.generation WHERE generation_id=%s", (gen,)).fetchone()
        if not row or row[0] not in ("ready", "retired"):
            raise IngestError(f"Generation {gen} is not ready (status={row[0] if row else 'missing'})")
        if not conn.execute("SELECT 1 FROM search_v2.asset_snapshot WHERE generation_id=%s LIMIT 1", (gen,)).fetchone():
            raise IngestError("Generation has no metadata snapshot; reimport the complete source folder before activation.")
        # Serialize first activation too (the singleton row may not exist yet).
        conn.execute("SELECT pg_advisory_xact_lock(78124496)")
        cur = conn.execute("SELECT generation_id FROM search_v2.active_generation WHERE singleton FOR UPDATE").fetchone()
        previous = cur[0] if cur else None
        if previous == gen:
            return
        _project_active_snapshot(conn, gen)
        conn.execute(
            """INSERT INTO search_v2.active_generation (singleton, generation_id, previous_generation_id, switched_at)
               VALUES (TRUE, %s, %s, now())
               ON CONFLICT (singleton) DO UPDATE SET generation_id=EXCLUDED.generation_id,
                 previous_generation_id=EXCLUDED.previous_generation_id, switched_at=now()""", (gen, previous))
        conn.execute("UPDATE search_v2.generation SET status='ready', activated_at=now() WHERE generation_id=%s", (gen,))
        if previous is not None:
            conn.execute("UPDATE search_v2.generation SET status='retired' WHERE generation_id=%s", (previous,))


def _project_active_snapshot(conn: psycopg.Connection, gen: int) -> None:
    """Atomically maintain V2-only compatibility tables; serving uses immutable snapshots."""
    from psycopg import sql
    conn.execute("DELETE FROM search_v2.asset")  # Cascades only to V2 scene/alias projections.
    for target, snapshot in (("asset", "asset_snapshot"), ("asset_alias", "alias_snapshot"), ("scene", "scene_snapshot")):
        columns = [r[0] for r in conn.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='search_v2' AND table_name=%s AND is_generated='NEVER'
               ORDER BY ordinal_position""", (target,))]
        names = sql.SQL(',').join(map(sql.Identifier, columns))
        conn.execute(sql.SQL('INSERT INTO search_v2.{} ({}) SELECT {} FROM search_v2.{} WHERE generation_id=%s')
                     .format(sql.Identifier(target), names, names, sql.Identifier(snapshot)), (gen,))


def rollback(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT previous_generation_id FROM search_v2.active_generation").fetchone()
    if not row or row[0] is None:
        raise IngestError("No previous generation to roll back to")
    switch_generation(conn, row[0])
    return row[0]


def prune_generations(conn: psycopg.Connection) -> int:
    """Keep the active generation, its rollback target and any build in progress; delete the rest."""
    with conn.transaction():
        cur = conn.execute(
            """DELETE FROM search_v2.generation g
               WHERE g.status IN ('retired','failed')
                 AND g.generation_id NOT IN (SELECT generation_id FROM search_v2.active_generation
                                             UNION SELECT previous_generation_id FROM search_v2.active_generation
                                             WHERE previous_generation_id IS NOT NULL)""")
        return cur.rowcount


# ---------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, help="Folder of Movie_*.json files, or a single file")
    ap.add_argument("--model", default=None, help="Embedding profile (default from EMBED_MODEL)")
    ap.add_argument("--hash-test", action="store_true", help="TESTS ONLY: hashing embedder, no model needed")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-activate", action="store_true")
    ap.add_argument("--activate", type=int, metavar="GENERATION")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--database-url", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = args.database_url or settings.database_url
    try:
        if args.rollback or args.activate:
            with psycopg.connect(url) as conn:
                gen = rollback(conn) if args.rollback else args.activate
                if args.activate:
                    switch_generation(conn, gen)
                print(f"Active generation is now {gen}")
            return 0
        if not args.data:
            ap.error("--data is required")
        embedder = load_embedder("hash-test" if args.hash_test else args.model, allow_hash=args.hash_test)
        report = ingest(args.data, embedder, dry_run=args.dry_run, activate=not args.no_activate, database_url=url)
    except (IngestError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
