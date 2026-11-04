"""V2 deadlines, cancellation ownership, pooling, cache and RRF; catalog-only serving."""
from __future__ import annotations
import asyncio
import logging
import time
from contextvars import ContextVar
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import numpy as np
import psycopg
from app.db import load_catalog, make_pool
from app.dense import SEED_SQL, dense_query
from app.embedding import Embedder
from app.fusion import AssetResult, Hit, fuse
from app.gazetteer import AssetInfo, Catalog
from app.lexical import main_query, stages
from app.planner import Plan, plan_query
from app.rerank import CrossEncoder
from app.text import scrub_pii
from config.settings import settings
log=logging.getLogger('service')
LEX_TIMEOUT_MS=450
DENSE_TIMEOUT_MS=600
MAX_LIMIT=50
_REQUEST_DEADLINE=ContextVar('deadline',default=None)
_RAW_DENSE=ContextVar('raw_dense',default=False)

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
    async def start(self):
        self.pool = make_pool()
        await self.pool.open(wait=True, timeout=10)
        await self._reload_catalog()

    async def _reload_catalog(self):
        def build():
            with psycopg.connect(settings.database_url, row_factory=psycopg.rows.dict_row,
                                 connect_timeout=2, options='-c statement_timeout=2000') as conn:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                return load_catalog(conn)
        cat = await asyncio.to_thread(build)
        self._catalog = cat
        self._catalog_loaded = self._gen_checked = time.monotonic()
        self._catalog_refresh_error = None
        self.dense_reason = self._dense_reason(cat)
        self.dense_ok = not self.dense_reason

    async def catalog(self):
        if self._catalog is not None and time.monotonic()-self._gen_checked < settings.generation_check_seconds:
            return self._catalog
        async with self._lock:
            if self._catalog is None:
                await self._reload_catalog()
            elif time.monotonic()-self._gen_checked >= settings.generation_check_seconds:
                self._gen_checked = time.monotonic()
                rows = await self._fetch('SELECT generation_id FROM schema_v2_large.active_generation', {}, 300)
                if not rows:
                    raise RuntimeError('No active large generation')
                if rows[0]['generation_id'] != self._catalog.generation['generation_id']:
                    await self._reload_catalog()
                self._catalog_refresh_error = None
        return self._catalog

    def _dense_reason(self, cat):
        g, e = cat.generation, self.embedder
        if not settings.dense_enabled or e is None:
            return 'No configured embedding model is loaded'
        if (g.get('embedding_model'),g.get('model_revision'),g.get('pooling'),g.get('query_prefix'),g.get('dim')) != (
            e.profile.name,e.revision,e.profile.pooling,e.profile.query_prefix,e.profile.dim):
            return 'Loaded model does not match this generation; dense retrieval is disabled'
        return ''

    def threshold_for(self, cat):
        if settings.dense_threshold_explicit:
            return settings.dense_min_similarity
        c, g = settings.calibration, cat.generation
        if not c:
            return None
        identity = dict(model=g.get('embedding_model'),revision=g.get('model_revision'),pooling=g.get('pooling'),
                        query_prefix=g.get('query_prefix'),text_version=g.get('text_version'),
                        passage_tokens=g.get('passage_tokens'),corpus_sha256=g.get('notes',{}).get('corpus_sha256'))
        return c.get('dense_min_similarity') if all(c.get(k)==v and v is not None for k,v in identity.items()) else None

    async def _execute(self, plan, cat, limit, deadline, debug, variant=(None,None)):
        resp = self._base(plan, cat)
        resp['schema'] = 'schema_v2_large'
        if plan.clarification:
            resp.update(status='needs_clarification',message=plan.clarification.message,
                        clarification={'reason':plan.clarification.reason,'suggestions':plan.clarification.suggestions})
            return resp
        if plan.empty_reason:
            resp.update(status='empty',message=plan.empty_reason)
            return resp
        start=time.perf_counter()
        try:
            if plan.mode in {'title','browse'}:
                keys = plan.allowed if plan.allowed is not None else cat.assets
                assets = [AssetResult(k) for k in sorted(keys,key=lambda k:(cat.assets[k].title_norm,k))]
            elif plan.mode == 'similar':
                assets = await deadline.run(self._similar(plan,cat,resp))
            else:
                assets = await self._retrieve(plan,cat,deadline,resp,variant)
            if self.reranker and assets and plan.mode not in {'title','browse'}:
                try:
                    assets = await deadline.run(self._rerank(plan,cat,assets,resp))
                except (asyncio.TimeoutError,RuntimeError,OSError):
                    resp['degraded'].append('reranker')
            assets = self._apply_sort(plan,cat,assets)[:min(plan.limit or limit,MAX_LIMIT)]
        except (asyncio.TimeoutError,psycopg.Error,OSError):
            assets=[]
            resp['degraded'].append('retrieval')
        resp['timings_ms']['retrieve']=round((time.perf_counter()-start)*1000,2)
        for rank,a in enumerate(assets,1):
            x=cat.assets[a.asset_key]
            row=cat.metadata.get(a.asset_key,{})
            card=dict(rank=rank,show_id=x.asset_id,title=x.title,plot_summary=x.description,
                genres=x.genres,themes=x.themes,audio_languages=x.audio_languages,cast=x.cast_names,
                primary_cast=row.get('primary_cast',[]),supporting_cast=row.get('supporting_cast',[]),
                directors=x.directors,characters=x.characters,release_date=x.release_date,
                thumbnail_url=x.thumbnail_image_url,matched_channels=sorted(a.ranks))
            if debug:
                card['debug']=dict(rrf=a.score,ranks=a.ranks,contributions=a.contributions)
            resp['results'].append(card)
        if resp.pop('_semantic_only',False) and assets:
            plan.approximate=True
            resp['notes'].append('These results are supported by semantic similarity only; no lexical match was found.')
        if resp['degraded']:
            resp.update(status='degraded',message='Some components were unavailable; results may be incomplete.')
        elif not assets:
            resp.update(status='empty',message='No matching movies found.')
        elif plan.approximate:
            resp['status']='approximate'
        resp['notes']=list(dict.fromkeys(resp['notes']))
        if debug:
            resp['debug']=dict(trace=plan.trace,channels=resp.pop('_channels',[]),
                               allowed=None if plan.allowed is None else len(plan.allowed))
        else:
            resp.pop('_channels',None)
        return resp

    async def _similar(self, plan, cat, resp):
        if self._dense_reason(cat):
            resp['degraded'].append('dense')
            return []
        rows=await self._fetch(SEED_SQL,dict(gen=cat.generation['generation_id'],keys=plan.similar_to),600)
        if not rows:
            return []
        vec=np.mean([np.fromstring(r['emb'].strip('[]'),sep=',',dtype=np.float32) for r in rows],axis=0)
        vec/=max(float(np.linalg.norm(vec)),1e-12)
        return await self._dense_hits(cat,vec,plan,exclude=plan.similar_to)

    async def _dense_hits(self, cat, vector, plan, exclude=None):
        q=dense_query(generation=cat.generation['generation_id'],vector=vector,window=settings.candidate_window,
                      allowed=sorted(plan.allowed) if plan.allowed is not None else None,exclude=exclude)
        rows=await self._fetch(q.sql,q.params,600)
        threshold=None if _RAW_DENSE.get() else self.threshold_for(cat)
        hits=[Hit(r['asset_key'],None,r['score']) for r in rows if threshold is None or r['score']>=threshold]
        return fuse({'catalog_dense':hits},{'catalog_dense':settings.semantic_weight},set(),window=settings.candidate_window)

    async def _retrieve(self, plan, cat, deadline, resp, variant):
        methods=variant[1]
        async def lexical():
            hits=[]
            for name,tsq in stages(plan.terms,settings.lex_relax):
                q=main_query(generation=cat.generation['generation_id'],tsquery=tsq,window=settings.candidate_window,
                             allowed=sorted(plan.allowed) if plan.allowed is not None else None)
                rows=await self._fetch(q.sql,q.params,450)
                hits=[Hit(r['asset_key'],None,r['score']) for r in rows]
                if len(hits)>=settings.lex_min_candidates:
                    break
            return hits
        async def dense():
            vec=await self._timed_embed(plan.dense_text or ' '.join(plan.terms),resp)
            # Convert single-channel fused assets back to raw channel order.
            rows=await self._dense_hits(cat,vec,plan)
            return [Hit(a.asset_key,None,a.score) for a in rows]
        tasks={}
        if methods is None or 'lex' in methods:
            tasks['catalog_lex']=asyncio.create_task(lexical())
        if methods is None or 'dense' in methods:
            reason=self._dense_reason(cat)
            if reason:
                resp['degraded'].append('dense')
                resp['notes'].append(reason)
            else:
                tasks['catalog_dense']=asyncio.create_task(dense())
        resp['_channels']=list(tasks)
        if not tasks:
            return []
        try:
            await asyncio.wait(tasks.values(),timeout=deadline.remaining())
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(),return_exceptions=True)
        hits={}
        for name,task in tasks.items():
            if task.cancelled() or task.exception():
                resp['degraded'].append(name)
            else:
                hits[name]=task.result()
        resp['_semantic_only']=not hits.get('catalog_lex') and bool(hits.get('catalog_dense'))
        return fuse(hits,dict(catalog_lex=settings.lexical_weight,catalog_dense=settings.semantic_weight),set(),
                    k=settings.rrf_k,window=settings.candidate_window)
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

    async def stop(self) -> None:
        if self.pool is not None:
            await self.pool.close()
        self._executor.shutdown(wait=False, cancel_futures=True)

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
                    await conn.execute('SET TRANSACTION READ ONLY')
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
        except (psycopg.Error, asyncio.TimeoutError, OSError, RuntimeError) as exc:
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

    async def _timed_embed(self, text: str, resp: dict[str, Any]) -> np.ndarray:
        t = time.perf_counter()
        vec = await self._embed(text)
        resp["timings_ms"]["embed"] = round((time.perf_counter() - t) * 1000, 1)
        return vec

    def _apply_sort(self, plan: Plan, cat: Catalog, assets: list[AssetResult]) -> list[AssetResult]:
        if plan.sort in ("latest", "oldest"):
            def key(a: AssetResult):
                x = cat.assets[a.asset_key]
                d = x.release_date or (date(x.release_year, 1, 1) if x.release_year else None)
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
