"""Search orchestration: plan -> concurrent channels -> weighted RRF -> hydrate -> response.

Latency design (V2 plan): one 950 ms request deadline; every channel has its own statement_timeout; a
channel that fails or times out is dropped and the response is marked ``degraded`` instead of failing;
a cancelled query is cancelled on the server (``cancel_safe``) so it cannot keep a pool slot busy;
query embedding runs in a small executor (concurrency 2) with a tiny LRU cache.

Statuses: ok | approximate | empty | needs_clarification | degraded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any
from urllib.parse import quote

import numpy as np
import psycopg

from app.db import load_catalog, make_pool
from app.dense import SEED_SQL, dense_query
from app.embedding import Embedder
from app.fusion import AssetResult, Hit, dedupe, fuse
from app.gazetteer import AssetInfo, Catalog
from app.lexical import dialogue_query, main_query, stages
from app.planner import ChannelSpec, Plan, build_channels, plan_query
from app.rerank import CrossEncoder
from app.text import normalize, scrub_pii
from app.vocab import FUNCTION_WORDS
from app.verify import verify_phrase
from config.settings import settings

log = logging.getLogger("service")
LEX_TIMEOUT_MS = 450
DENSE_TIMEOUT_MS = 600
MAX_LIMIT = 50
_REQUEST_DEADLINE: ContextVar[Deadline | None] = ContextVar("search_deadline", default=None)
_RAW_DENSE: ContextVar[bool] = ContextVar("raw_dense_scores", default=False)


@contextmanager
def raw_dense_scores():
    """Collect raw nearest-neighbour scores for calibration without mutating global settings."""
    token = _RAW_DENSE.set(True)
    try:
        yield
    finally:
        _RAW_DENSE.reset(token)

HYDRATE_SQL = """
SELECT s.asset_key, s.scene_id, s.scene_order, s.start_time, s.end_time, s.start_seconds, s.end_seconds,
       s.description, s.master_summary, s.visual_summary, s.dialogue_turns, s.mood, s.cast_names, s.cast_norm,
       s.characters, s.characters_norm, s.objects, s.tags
FROM search_v2.scene_snapshot s
JOIN unnest(%(keys)s::text[], %(ids)s::text[]) AS t(asset_key, scene_id) USING (asset_key, scene_id)
WHERE s.generation_id = %(gen)s"""


class Deadline:
    def __init__(self, ms: float) -> None:
        self.end = time.perf_counter() + ms / 1000.0

    def remaining(self) -> float:
        return max(self.end - time.perf_counter(), 0.0)

    async def run(self, operation):
        remaining = self.remaining()
        if remaining <= 0:
            if hasattr(operation, "close"):
                operation.close()
            raise asyncio.TimeoutError("request deadline exhausted")
        return await asyncio.wait_for(operation, remaining)


class SearchService:
    def __init__(self, embedder: Embedder | None, reranker: CrossEncoder | None = None) -> None:
        self.embedder = embedder
        self.reranker = reranker if settings.rerank_enabled else None
        self.pool = None
        self._catalog: Catalog | None = None
        self._catalog_loaded = 0.0
        self._gen_checked = 0.0
        self._catalog_refresh_error: str | None = None
        self._lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max(settings.embed_concurrency, 1), thread_name_prefix="embed")
        self._embed_sem = asyncio.Semaphore(max(settings.embed_concurrency, 1))
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self.dense_ok = False
        self.dense_reason = ""

    # -- lifecycle ------------------------------------------------------------------------------
    async def start(self) -> None:
        self.pool = make_pool()
        await self.pool.open(wait=True, timeout=10)
        await self._reload_catalog()

    async def stop(self) -> None:
        if self.pool is not None:
            await self.pool.close()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- catalog snapshot -----------------------------------------------------------------------
    async def _reload_catalog(self) -> None:
        def build() -> Catalog:
            budget = _REQUEST_DEADLINE.get()
            timeout_ms = max(1, int(min(budget.remaining() * 1000, 950))) if budget else 950
            with psycopg.connect(settings.database_url, row_factory=psycopg.rows.dict_row, connect_timeout=1,
                                 options=f"-c statement_timeout={timeout_ms}") as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                return load_catalog(conn)

        cat = await asyncio.to_thread(build)
        self._catalog, self._catalog_loaded, self._gen_checked = cat, time.monotonic(), time.monotonic()
        self._catalog_refresh_error = None
        self._check_dense(cat)
        log.info("catalog loaded: %s", cat.summary())

    def _check_dense(self, cat: Catalog) -> None:
        g, e = cat.generation, self.embedder
        if not settings.dense_enabled:
            self.dense_ok, self.dense_reason = False, "dense search is disabled in the configuration"
        elif e is None:
            self.dense_ok, self.dense_reason = False, "no embedding model is loaded"
        elif (g.get("embedding_model"), g.get("model_revision"), g.get("pooling"), g.get("query_prefix")) != (
                e.profile.name, e.revision, e.profile.pooling, e.profile.query_prefix):
            self.dense_ok = False
            self.dense_reason = (f"the index was built with {g.get('embedding_model')} but {e.profile.name} is "
                                 "loaded; rebuild the index (python -m app.ingest)")
        else:
            self.dense_ok, self.dense_reason = True, ""
        if not self.dense_ok:
            log.warning("Dense retrieval off: %s", self.dense_reason)
        elif settings.dense_min_similarity is None:
            log.warning("Dense retrieval has NO similarity cutoff (not calibrated for %s): requests that match "
                        "nothing in the catalog will still return their nearest neighbours. Run "
                        "calibration guidance in README.md before production use.",
                        self.embedder.profile.name if self.embedder else "?")
        if self.calibration_reason:
            log.warning("Dense cutoff ignored: %s", self.calibration_reason)

    async def catalog(self) -> Catalog:
        now = time.monotonic()
        if self._catalog is not None and now - self._gen_checked < settings.generation_check_seconds:
            return self._catalog
        async with self._lock:
            if self._catalog is None:
                await self._reload_catalog()
                return self._catalog                                     # type: ignore[return-value]
            now = time.monotonic()
            if now - self._gen_checked >= settings.generation_check_seconds:
                self._gen_checked = now
                row = await self._fetch("SELECT generation_id FROM search_v2.active_generation", {}, 300)
                current = row[0]["generation_id"] if row else None
                stale = now - self._catalog_loaded > settings.catalog_cache_seconds
                if current != self._catalog.generation.get("generation_id") or stale:
                    await self._reload_catalog()
                self._catalog_refresh_error = None
        return self._catalog                                              # type: ignore[return-value]

    # -- database ---------------------------------------------------------------------------------
    async def _fetch(self, sql: str, params: dict[str, Any], timeout_ms: int) -> list[dict[str, Any]]:
        assert self.pool is not None
        deadline = _REQUEST_DEADLINE.get()
        remaining = deadline.remaining() if deadline else max(timeout_ms / 1000, 0.001)
        if remaining <= 0:
            raise asyncio.TimeoutError("request deadline exhausted")
        timeout_ms = max(1, min(timeout_ms, int(remaining * 1000)))
        async with self.pool.connection(timeout=remaining) as conn:
            try:
                async with conn.transaction():
                    if deadline is not None:
                        if deadline.remaining() <= 0:
                            raise asyncio.TimeoutError("request deadline exhausted")
                        timeout_ms = max(1, min(timeout_ms, int(deadline.remaining() * 1000)))
                    await conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),))
                    cur = await conn.execute(sql, params)
                    return await cur.fetchall()
            except asyncio.CancelledError:
                # psycopg >= 3.3 already cancels and recycles the connection when the awaiting task is
                # cancelled (checked: tests/test_integration.py::test_13); this explicit, shielded cancel is a
                # safeguard for 3.2.x, which is untested here. Either way the query must not keep running.
                try:
                    await asyncio.shield(conn.cancel_safe(timeout=0.1))
                except Exception:                      # noqa: BLE001
                    await conn.close()
                raise

    # -- embedding --------------------------------------------------------------------------------
    async def _inference(self, function, *args):
        """Keep admission capacity until the thread really finishes, even after cancellation."""
        await self._embed_sem.acquire()
        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, function, *args)
        except BaseException:
            self._embed_sem.release()
            raise
        # Cancelling the awaiting request cannot stop an active ONNX call. Shield its
        # future, and release capacity on completion instead of on request cancellation.
        def completed(task):
            self._embed_sem.release()
            if not task.cancelled():
                task.exception()  # observe failures even when the owning request was abandoned
        future.add_done_callback(completed)
        return await asyncio.shield(future)

    async def _embed(self, text: str) -> np.ndarray:
        if text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        assert self.embedder is not None
        vec = await self._inference(self.embedder.encode_query, text)
        self._cache[text] = vec
        if len(self._cache) > 512:
            self._cache.popitem(last=False)
        return vec

    # -- public -------------------------------------------------------------------------------------
    async def search(self, query: str, limit: int = 10, debug: bool = False, *, profile: str | None = None,
                     methods: set[str] | None = None, raw_dense: bool = False) -> dict[str, Any]:
        deadline = Deadline(settings.request_deadline_ms)
        deadline_token = _REQUEST_DEADLINE.set(deadline)
        raw_token = _RAW_DENSE.set(raw_dense or _RAW_DENSE.get())
        try:
            return await self._search(query, limit, debug, profile=profile, methods=methods, deadline=deadline)
        finally:
            _RAW_DENSE.reset(raw_token)
            _REQUEST_DEADLINE.reset(deadline_token)

    async def _search(self, query: str, limit: int, debug: bool, *, profile: str | None,
                      methods: set[str] | None, deadline: Deadline) -> dict[str, Any]:
        """``profile`` / ``methods`` exist for benchmarking (lexical-only, dense-only, equal weights)."""
        variant = (profile, methods)
        t0 = time.perf_counter()
        catalog_error = None
        try:
            cat = await deadline.run(self.catalog())
        except (psycopg.Error, asyncio.TimeoutError, OSError) as exc:
            catalog_error = exc.__class__.__name__
            self._catalog_refresh_error = catalog_error
            cat = self._catalog
            if cat is None:
                return {"query": scrub_pii(query)[0], "status": "degraded", "results": [],
                        "message": "The catalog is temporarily unavailable. Please try again.", "notes": [],
                        "interpretation": {}, "clarification": None, "degraded": ["catalog"],
                        "timings_ms": {"total": round((time.perf_counter() - t0) * 1000, 1)}}
        plan = plan_query(query, cat, date.today())
        resp = await self._execute(plan, cat, limit, deadline, debug, variant)
        # a clarification with a safe preview: run the suggested query, labelled, without rewriting the user's
        if plan.clarification and plan.clarification.preview_query and deadline.remaining() > 0:
            prev_plan = plan_query(plan.clarification.preview_query, cat, date.today())
            if prev_plan.clarification is None and prev_plan.mode != "clarify":
                prev = await self._execute(prev_plan, cat, settings.preview_results, deadline, False, variant)
                resp["preview"] = {"label": f"Closest match for \"{plan.clarification.preview_query}\"",
                                   "query": plan.clarification.preview_query, "status": prev["status"],
                                   "results": prev["results"]}
        if catalog_error or self._catalog_refresh_error:
            resp["status"] = "degraded"
            resp["degraded"].append("catalog")
            resp["notes"].append("Catalog refresh failed; results use the last complete snapshot.")
        resp["timings_ms"]["total"] = round((time.perf_counter() - t0) * 1000, 1)
        log.info("search q=%r mode=%s status=%s results=%d %.0fms", plan.scrubbed, plan.mode, resp["status"],
                 len(resp["results"]), resp["timings_ms"]["total"])
        return resp

    # -- core ----------------------------------------------------------------------------------------
    def _base(self, plan: Plan, cat: Catalog) -> dict[str, Any]:
        return {
            "query": plan.scrubbed, "status": "ok", "message": None, "notes": list(plan.notes), "results": [],
            "interpretation": {
                "mode": plan.mode, "filters": [f.label() for f in plan.facets],
                "years": plan.years.describe() if plan.years else None, "sort": plan.sort, "limit": plan.limit,
                "terms": plan.terms, "phrase": plan.phrase,
                "corrections": [{"typed": c.original, "used": c.corrected, "kind": c.kind, "method": c.method}
                                for c in plan.corrections]},
            "clarification": None, "timings_ms": {}, "generation": cat.generation.get("generation_id"),
            "degraded": [],
        }

    async def _execute(self, plan: Plan, cat: Catalog, limit: int, deadline: Deadline, debug: bool,
                       variant: tuple[str | None, set[str] | None] = (None, None)) -> dict[str, Any]:
        resp = self._base(plan, cat)
        t = time.perf_counter()
        if plan.clarification is not None:
            c = plan.clarification
            resp.update(status="needs_clarification", message=c.message,
                        clarification={"reason": c.reason, "suggestions": c.suggestions})
            return resp
        if plan.empty_reason:
            resp.update(status="empty", message=plan.empty_reason)
            return resp
        n = min(plan.limit or limit, MAX_LIMIT)
        assets: list[AssetResult]
        scenes_by_asset: dict[str, list[dict[str, Any]]] = {}
        try:
            if plan.mode in ("title", "browse"):
                assets = self._listing(plan, cat)
            elif plan.mode == "similar":
                assets = await self._similar(plan, cat, deadline, resp)
            else:
                assets = await self._retrieve(plan, cat, deadline, resp, variant)
        except (psycopg.Error, asyncio.TimeoutError, OSError) as exc:
            log.warning("search failed: %s", exc.__class__.__name__)
            resp.update(status="degraded", message="Search is temporarily unavailable. Please try again.")
            resp["degraded"].append(exc.__class__.__name__)
            return resp
        resp["timings_ms"]["retrieve"] = round((time.perf_counter() - t) * 1000, 1)

        if self.reranker is not None and assets and plan.mode not in ("title", "browse"):
            try:
                assets = await deadline.run(self._rerank(plan, cat, assets, resp))
            except (asyncio.TimeoutError, RuntimeError, OSError):
                resp["degraded"].append("rerank")
        # Explicit user ordering is authoritative, including after optional relevance reranking.
        assets = self._apply_sort(plan, cat, assets)
        top = assets[:n]
        t = time.perf_counter()
        if plan.mode not in ("title", "browse", "similar"):
            try:
                scenes_by_asset = await deadline.run(self._hydrate_scenes(plan, top, deadline, resp))
            except (asyncio.TimeoutError, psycopg.Error, OSError):
                resp["degraded"].append("scene_details")
        resp["timings_ms"]["hydrate"] = round((time.perf_counter() - t) * 1000, 1)
        resp["results"] = [self._card(rank, a, cat, scenes_by_asset.get(a.asset_key, []), debug)
                           for rank, a in enumerate(top, start=1)]
        self._finalize(plan, resp, scenes_by_asset)
        top_dense, channel_names = resp.pop("_top_dense", None), resp.pop("_channels", [])
        if debug and (settings.debug_diagnostics or _RAW_DENSE.get()):
            resp["debug"] = {"trace": plan.trace, "channels": channel_names,
                             "top_dense_score": top_dense,
                             "allowed": None if plan.allowed is None else len(plan.allowed),
                             "dense_ok": self.dense_ok}
        return resp

    # -- modes ------------------------------------------------------------------------------------------
    def _listing(self, plan: Plan, cat: Catalog) -> list[AssetResult]:
        keys = sorted(plan.allowed if plan.allowed is not None else cat.assets)
        exact_title = {k for f in plan.facets if f.kind == "title" for e in f.alternatives for k in cat.assets_for(e)}
        keys.sort(key=lambda k: (k not in exact_title, cat.assets[k].title_norm, cat.assets[k].language))
        return [AssetResult(k, score=0.0) for k in keys]

    async def _similar(self, plan: Plan, cat: Catalog, deadline: Deadline, resp: dict[str, Any]) -> list[AssetResult]:
        if not self.dense_ok:
            resp["degraded"].append("dense")
            resp["notes"].append(f"Similar-title search is unavailable: {self.dense_reason}.")
            return []
        gen = cat.generation["generation_id"]
        rows = await deadline.run(self._fetch(SEED_SQL, {"gen": gen, "keys": plan.similar_to}, DENSE_TIMEOUT_MS))
        vecs = [np.asarray([float(x) for x in r["emb"].strip("[]").split(",")], dtype=np.float32) for r in rows]
        if not vecs:
            return []
        seed = np.mean(vecs, axis=0)
        seed = seed / (np.linalg.norm(seed) or 1.0)
        allowed = sorted(plan.allowed) if plan.allowed is not None else None
        q = dense_query(generation=gen, scope="catalog", kinds=("main",), vector=seed, window=settings.candidate_window,
                        allowed=allowed, exclude=plan.similar_to)
        out = await deadline.run(self._fetch(q.sql, q.params, DENSE_TIMEOUT_MS))
        hits = self._apply_threshold([Hit(r["asset_key"], r["scene_id"], r["score"]) for r in out])
        return fuse({"catalog_dense": hits}, {"catalog_dense": 1.0}, set(), k=settings.rrf_k, window=settings.candidate_window)

    @property
    def calibration_reason(self) -> str:
        calibration = getattr(settings, "calibration", {})
        if not calibration or getattr(settings, "dense_threshold_explicit", False):
            return ""
        gen = getattr(getattr(self, "_catalog", None), "generation", {})
        expected = {"revision": gen.get("model_revision"), "text_version": gen.get("text_version"),
                    "corpus_sha256": (gen.get("notes") or {}).get("corpus_sha256")}
        if (calibration.get("model") != gen.get("embedding_model")
                or calibration.get("pooling") != gen.get("pooling")
                or calibration.get("query_prefix") != gen.get("query_prefix")
                or any(calibration.get(key) != gen.get(key) for key in
                       ("dim", "passage_tokens", "overlap_sentences", "scene_header"))
                or any(not value or calibration.get(key) != value for key, value in expected.items())):
            return "calibration does not match the active model, text version and corpus; recalibrate"
        return ""

    @property
    def dense_threshold(self) -> float | None:
        # A stored threshold describes one complete index identity. Explicit runtime
        # overrides remain available for operations/tests, but stale files never filter.
        return None if SearchService.calibration_reason.fget(self) else settings.dense_min_similarity

    def _apply_threshold(self, hits: list[Hit]) -> list[Hit]:
        if _RAW_DENSE.get():
            return hits
        thr = SearchService.dense_threshold.fget(self)
        return [h for h in hits if thr is None or h.score >= thr]

    async def _retrieve(self, plan: Plan, cat: Catalog, deadline: Deadline, resp: dict[str, Any],
                        variant: tuple[str | None, set[str] | None] = (None, None)) -> list[AssetResult]:
        channels = build_channels(plan, *variant)
        resp["_channels"] = [c.name for c in channels]
        if not channels:
            return []
        gen = cat.generation["generation_id"]
        allowed = sorted(plan.allowed) if plan.allowed is not None else None
        window = settings.candidate_window
        dense_wanted = any(c.method == "dense" for c in channels)
        if dense_wanted and not self.dense_ok:
            channels = [c for c in channels if c.method != "dense"]
            resp["degraded"].append("dense")
            resp["notes"].append(f"Semantic search is off: {self.dense_reason}.")
        if not channels:
            return []
        dense_text = plan.phrase if (plan.mode == "quote" and plan.phrase) else plan.dense_text
        emb_task = None
        if any(c.method == "dense" for c in channels) and dense_text:
            emb_task = asyncio.create_task(self._timed_embed(dense_text, resp))
        tasks = {c.name: asyncio.create_task(self._channel(c, plan, gen, allowed, window, emb_task)) for c in channels}
        try:
            done, pending = await asyncio.wait(set(tasks.values()), timeout=deadline.remaining())
        finally:
            # This also runs when the parent HTTP/request task is cancelled while waiting.
            owned = [*tasks.values(), *([emb_task] if emb_task is not None else [])]
            unfinished = [task for task in owned if not task.done()]
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
        hits: dict[str, list[Hit]] = {}
        stages_used: dict[str, str] = {}
        for c in channels:
            task = tasks[c.name]
            if task in pending or task.cancelled():
                resp["degraded"].append(f"{c.name}: timeout")
                continue
            exc = task.exception()
            if exc is not None:
                resp["degraded"].append(f"{c.name}: {exc.__class__.__name__}")
                log.warning("channel %s failed: %s", c.name, exc)
                continue
            h, stage = task.result()
            hits[c.name] = h
            if stage:
                stages_used[c.name] = stage
        resp["_stages"] = stages_used
        tops = [hits[c.name][0].score for c in channels if c.method == "dense" and hits.get(c.name)]
        resp["_top_dense"] = max(tops) if tops else None
        lex_names = [c.name for c in channels if c.method in ("lex", "phrase") and c.name in hits]
        dense_names = [c.name for c in channels if c.method == "dense" and c.name in hits]
        resp["_semantic_only"] = bool(lex_names and dense_names and not any(hits[n] for n in lex_names)
                                      and any(hits[n] for n in dense_names))
        weights = {c.name: c.weight for c in channels}
        scene_channels = {c.name for c in channels if c.scope == "scene"}
        return fuse(hits, weights, scene_channels, k=settings.rrf_k, window=window,
                    bonus=settings.multi_scene_bonus, bonus_scenes=settings.multi_scene_bonus_scenes)

    async def _timed_embed(self, text: str, resp: dict[str, Any]) -> np.ndarray:
        t = time.perf_counter()
        vec = await self._embed(text)
        resp["timings_ms"]["embed"] = round((time.perf_counter() - t) * 1000, 1)
        return vec

    async def _channel(self, c: ChannelSpec, plan: Plan, gen: int, allowed: list[str] | None, window: int,
                       emb_task: asyncio.Task | None) -> tuple[list[Hit], str | None]:
        by_scene = c.scope == "scene"
        if c.method == "dense":
            if emb_task is None:
                return [], None
            vec = await asyncio.shield(emb_task)
            q = dense_query(generation=gen, scope=c.scope, kinds=c.kinds, vector=vec, window=window, allowed=allowed)
            rows = await self._fetch(q.sql, q.params, DENSE_TIMEOUT_MS)
            hits = [Hit(r["asset_key"], r["scene_id"], r["score"]) for r in rows]
            return dedupe(self._apply_threshold(hits), by_scene), None
        if c.method == "phrase":
            assert plan.phrase
            rows = await self._fetch(*self._q(dialogue_query(generation=gen, tsquery=plan.phrase, window=window,
                                                              allowed=allowed, phrase=True)), LEX_TIMEOUT_MS)
            hits = [Hit(r["asset_key"], r["scene_id"], r["score"]) for r in rows]
            stage = "phrase"
            if len(dedupe(hits, True)) < settings.lex_min_candidates:
                rows = await self._fetch(*self._q(dialogue_query(generation=gen, tsquery=plan.phrase, window=window,
                                                                  allowed=allowed, plain=True)), LEX_TIMEOUT_MS)
                hits += [Hit(r["asset_key"], r["scene_id"], r["score"]) for r in rows]
                stage = "phrase+all_words"
            return dedupe(hits, True), stage
        # lexical, staged relaxation
        terms = plan.terms
        if c.name == "dialogue_lex" and plan.phrase:
            terms = [w for w in normalize(plan.phrase).split() if w not in FUNCTION_WORDS] or normalize(plan.phrase).split()
        best: list[Hit] = []
        used = None
        for name, tsq in stages(terms, settings.lex_relax):
            if c.kinds == ("dialogue",):
                q = dialogue_query(generation=gen, tsquery=tsq, window=window, allowed=allowed)
            else:
                q = main_query(generation=gen, scope=c.scope, tsquery=tsq, window=window, allowed=allowed)
            rows = await self._fetch(q.sql, q.params, LEX_TIMEOUT_MS)
            best = dedupe([Hit(r["asset_key"], r["scene_id"], r["score"]) for r in rows], by_scene)
            used = name
            if len(best) >= settings.lex_min_candidates:
                break
        return best, used

    @staticmethod
    def _q(q) -> tuple[str, dict]:
        return q.sql, q.params

    # -- ordering, reranking ------------------------------------------------------------------------------
    def _apply_sort(self, plan: Plan, cat: Catalog, assets: list[AssetResult]) -> list[AssetResult]:
        if plan.sort in ("latest", "oldest"):
            def key(a: AssetResult):
                x = cat.assets[a.asset_key]
                d = x.original_release_date or (date(x.original_release_year, 1, 1) if x.original_release_year else None)
                missing = d is None
                ordinal = d.toordinal() if d else 0
                return (missing, -ordinal if plan.sort == "latest" else ordinal, -a.score, a.asset_key)
            return sorted(assets, key=key)
        if plan.sort == "added":
            return sorted(assets, key=lambda a: (cat.assets[a.asset_key].platform_release_date is None,
                                                 -(cat.assets[a.asset_key].platform_release_date or date.min).toordinal(),
                                                 -a.score, a.asset_key))
        return assets

    async def _rerank(self, plan: Plan, cat: Catalog, assets: list[AssetResult], resp: dict[str, Any]) -> list[AssetResult]:
        depth = min(settings.rerank_depth, len(assets))
        head = assets[:depth]
        texts = [f"{cat.assets[a.asset_key].title}. {cat.assets[a.asset_key].description}" for a in head]
        query = plan.dense_text or " ".join(plan.terms) or plan.phrase or plan.normalized
        t = time.perf_counter()
        scores = await self._inference(self.reranker.score, query, texts)
        resp["timings_ms"]["rerank"] = round((time.perf_counter() - t) * 1000, 1)
        order = sorted(range(depth), key=lambda i: (-scores[i], head[i].asset_key))
        return [head[i] for i in order] + assets[depth:]

    # -- hydration ------------------------------------------------------------------------------------------
    async def _hydrate_scenes(self, plan: Plan, top: list[AssetResult], deadline: Deadline,
                              resp: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        pairs = [(a.asset_key, s.scene_id) for a in top for s in a.scenes[:settings.scenes_per_card * 2]]
        if not pairs:
            return {}
        try:
            rows = await deadline.run(self._fetch(HYDRATE_SQL,
                {"keys": [p[0] for p in pairs], "ids": [p[1] for p in pairs], "gen": resp["generation"]}, LEX_TIMEOUT_MS))
        except (psycopg.Error, asyncio.TimeoutError):
            resp["degraded"].append("scene_details")
            return {}
        by_key = {(r["asset_key"], r["scene_id"]): r for r in rows}
        out: dict[str, list[dict[str, Any]]] = {}
        for a in top:
            items = []
            for s in a.scenes[:settings.scenes_per_card * 2]:
                r = by_key.get((a.asset_key, s.scene_id))
                if r is None:
                    continue
                literal = None
                if plan.phrase:
                    literal = verify_phrase(r["dialogue_turns"] or [], plan.phrase, settings.dialogue_across_turns)
                actor_hit = bool(plan.scene_actor_terms and (set(plan.scene_actor_terms) & (set(r["cast_norm"]) | set(r["characters_norm"]))))
                items.append((literal is None, not actor_hit, -s.score, r["scene_order"], r, s, literal, actor_hit))
            items.sort(key=lambda x: x[:4])
            out[a.asset_key] = [self._scene_card(x[4], x[5], x[6], x[7]) for x in items[:settings.scenes_per_card]]
        return out

    def _scene_card(self, r: dict[str, Any], s, literal: str | None, actor_hit: bool) -> dict[str, Any]:
        asset_id, language = r["asset_key"].split("|", 1)
        return {
            "scene_id": r["scene_id"], "start_time": r["start_time"], "end_time": r["end_time"],
            "start_seconds": r["start_seconds"], "end_seconds": r["end_seconds"],
            "description": r["description"], "summary": r["master_summary"], "mood": r["mood"],
            "dialogue": r["dialogue_turns"], "cast": r["cast_names"], "characters": r["characters"],
            "objects": r["objects"], "tags": r["tags"],
            "literal_match": literal, "actor_in_scene": actor_hit if actor_hit else None,
            "matched_channels": sorted(s.ranks),
            "playback_url": f"/api/video/{quote(asset_id, safe='')}?language={quote(language, safe='')}&start={r['start_seconds']:g}",
        }

    def _card(self, rank: int, a: AssetResult, cat: Catalog, scenes: list[dict[str, Any]], debug: bool) -> dict[str, Any]:
        x: AssetInfo = cat.assets[a.asset_key]
        card = {
            "rank": rank, "asset_id": x.asset_id, "language": x.language, "title": x.title,
            "content_type": x.content_type, "genres": x.genres, "cast": x.cast_names,
            "description": x.description, "thumbnail_url": x.thumbnail_image_url,
            "original_release_year": x.original_release_year,
            "total_scenes": x.total_scenes, "scenes": scenes,
            "playback_url": f"/api/video/{quote(x.asset_id, safe='')}?language={quote(x.language, safe='')}&start=0",
            "matched_channels": sorted(a.ranks),
        }
        if debug:
            card["debug"] = {"rrf": round(a.score, 6), "ranks": a.ranks,
                             "contributions": {k: round(v, 6) for k, v in a.contributions.items()}}
        return card

    # -- status -------------------------------------------------------------------------------------------
    def _finalize(self, plan: Plan, resp: dict[str, Any], scenes_by_asset: dict[str, list[dict[str, Any]]]) -> None:
        stages_used = resp.pop("_stages", {})
        results = resp["results"]
        if plan.phrase:
            verified = any(s.get("literal_match") for r in results for s in r["scenes"])
            if results and not verified:
                resp["notes"].append(f"No scene contains the exact words \"{plan.phrase}\"; showing the closest matches.")
                plan.approximate = True
        relaxed = [ch for ch, v in stages_used.items() if v in ("relaxed", "but_one")
                   and not (ch.startswith("dialogue") and plan.phrase)]
        if relaxed:
            resp["notes"].append("Not every search word was found together; results match as many as possible.")
        if resp.pop("_semantic_only", False) and results:
            resp["notes"].append("None of the search words appear in the catalog text; these are the closest matches "
                                 "by meaning.")
            plan.approximate = True
        if resp["degraded"]:
            resp["status"] = "degraded"
            resp["message"] = "Some search components were unavailable, so results may be incomplete."
        elif not results:
            resp["status"] = "empty"
            resp["message"] = "No matching titles or scenes found."
        elif plan.approximate:
            resp["status"] = "approximate"
        resp["notes"] = list(dict.fromkeys(resp["notes"]))
        resp["stages"] = stages_used
