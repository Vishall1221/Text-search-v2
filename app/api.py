"""HTTP API (FastAPI) and the small browser UI. Port 8001 by default; V1 keeps its own port."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.embedding import load_embedder
from app.rerank import load_reranker
from app.service import SearchService
from app.video import video_response
from config.settings import settings

log = logging.getLogger("api")
FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    embedder = None
    if settings.dense_enabled:
        try:
            embedder = load_embedder()
        except (FileNotFoundError, KeyError, RuntimeError) as exc:
            log.warning("Semantic search disabled: %s", exc)
    reranker = load_reranker(settings.rerank_model_dir, settings.embed_threads) if settings.rerank_enabled else None
    service = SearchService(embedder, reranker)
    try:
        await service.start()
        app.state.service = service
        yield
    finally:
        await service.stop()


app = FastAPI(title="Text Search v2", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    debug: bool = False


@app.get("/")
def home() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness only: the process is up. Use /api/ready for dependencies."""
    return {"status": "ok"}


@app.get("/api/ready")
async def ready(request: Request):
    svc: SearchService = request.app.state.service
    try:
        cat = await svc.catalog()
        row = await svc._fetch("SELECT 1 AS ok", {}, 500)                      # noqa: SLF001
    except Exception as exc:                                                    # noqa: BLE001
        return JSONResponse({"status": "unavailable", "reason": exc.__class__.__name__}, status_code=503)
    body = {"status": "ready" if row else "unavailable", "generation": cat.generation.get("generation_id"),
            "assets": len(cat.assets), "documents": cat.generation.get("doc_count"),
            "embedding_model": cat.generation.get("embedding_model"), "dense": svc.dense_ok,
            "dense_reason": svc.dense_reason or None, "reranker": svc.reranker is not None,
            "dense_threshold": svc.dense_threshold,
            "dense_calibrated": svc.dense_threshold is not None,
            "calibration_reason": svc.calibration_reason or None}
    return JSONResponse(body, status_code=200 if row else 503)


@app.post("/api/search")
async def search(payload: SearchRequest, request: Request) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(422, "Query cannot be blank")
    svc: SearchService = request.app.state.service
    return await svc.search(query, payload.limit, payload.debug)


@app.get("/api/video/{asset_id}")
def video(asset_id: str, request: Request, language: str = Query(min_length=2)):
    try:
        return video_response(asset_id, language, request.headers.get("range"))
    except psycopg.Error as exc:
        raise HTTPException(503, f"Database unavailable: {exc.__class__.__name__}") from exc
