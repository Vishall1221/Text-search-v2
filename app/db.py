"""Database access: async pool for the service, plain helpers for the catalog snapshot."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.gazetteer import AssetInfo, Catalog, asset_from_row
from config.settings import settings


def make_pool() -> AsyncConnectionPool:
    return AsyncConnectionPool(
        settings.database_url, min_size=settings.db_pool_min, max_size=settings.db_pool_max, open=False,
        kwargs={"row_factory": dict_row, "autocommit": True}, timeout=2.0, name="search_v2")


ASSET_SQL = """SELECT asset_id, language, title, title_norm, description, genres, genres_norm, cast_names, cast_norm,
                      characters, directors, content_type, original_release_year, original_release_date,
                      platform_release_date, movie_gcs_path, thumbnail_image_url, total_scenes
               FROM search_v2.asset_snapshot WHERE generation_id=%s ORDER BY title_norm, language"""


def active_generation(conn: psycopg.Connection) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT g.* FROM search_v2.generation g JOIN search_v2.active_generation a USING (generation_id)"""
    ).fetchone()


def load_catalog(conn: psycopg.Connection) -> Catalog:
    """Build the in-memory snapshot of the ACTIVE generation (rows must come back as dicts)."""
    gen = active_generation(conn)
    if gen is None:
        raise RuntimeError("No active index generation. Run: python -m app.ingest --data <folder>")
    gid = gen["generation_id"]
    aliases: dict[tuple[str, str], list[str]] = {}
    for r in conn.execute("SELECT asset_id, language, alias FROM search_v2.alias_snapshot WHERE generation_id=%s", (gid,)):
        aliases.setdefault((r["asset_id"], r["language"]), []).append(r["alias"])
    assets: list[AssetInfo] = [
        asset_from_row(r, aliases.get((r["asset_id"], r["language"]), [])) for r in conn.execute(ASSET_SQL, (gid,))]
    if not assets:
        raise RuntimeError("This V2 generation predates metadata snapshots; reimport the complete source folder.")
    lexicon = {r["word"]: r["df"] for r in conn.execute(
        "SELECT word, df FROM search_v2.lexicon WHERE generation_id=%s", (gen["generation_id"],))}
    return Catalog(assets, lexicon=lexicon, generation=gen, doc_total=gen["doc_count"])


def load_catalog_sync(url: str | None = None) -> Catalog:
    with psycopg.connect(url or settings.database_url, row_factory=dict_row, connect_timeout=2) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '500ms'")
        return load_catalog(conn)
