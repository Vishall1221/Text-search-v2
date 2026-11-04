"""Catalog-only API and UI. Startup is read-only; default port 8002."""
from contextlib import asynccontextmanager
from pathlib import Path
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from app.embedding import load_embedder
from app.service import SearchService
from app.rerank import load_reranker
from config.settings import settings

@asynccontextmanager
async def lifespan(app):
    encoder=None
    if settings.dense_enabled:
        try:
            encoder=load_embedder()
        except (OSError,RuntimeError,KeyError) as exc:
            logging.warning('Dense model unavailable: %s',type(exc).__name__)
    reranker=load_reranker(settings.rerank_model_dir) if settings.rerank_enabled else None
    svc=SearchService(encoder,reranker)
    try:
        await svc.start()
        app.state.service=svc
        yield
    finally:
        await svc.stop()

app=FastAPI(title='Text Search V2 Large — catalog only',version='1.0.0',lifespan=lifespan)
FRONTEND=Path(__file__).resolve().parents[1]/'frontend'
app.mount('/static',StaticFiles(directory=FRONTEND),name='static')

class SearchRequest(BaseModel):
    query:str=Field(min_length=1,max_length=500)
    limit:int=Field(default=10,ge=1,le=50)
    debug:bool=False

@app.get('/')
def home():
    return FileResponse(FRONTEND/'index.html')

@app.get('/api/health')
def health():
    return {'status':'ok'}

@app.get('/api/ready')
async def ready(request:Request):
    svc=request.app.state.service
    try:
        cat=await svc.catalog()
        await svc._fetch('SELECT 1 AS ok',{},500)
        return dict(status='ready',schema='schema_v2_large',generation=cat.generation['generation_id'],
                    assets=len(cat.assets),documents=cat.generation['doc_count'],actor_aliases=len(cat.actor_aliases),
                    lexicon_words=len(cat.lexicon),dense=not svc._dense_reason(cat),
                    dense_reason=svc._dense_reason(cat) or None,embedding_model=cat.generation['embedding_model'],
                    dense_calibrated=svc.threshold_for(cat) is not None,date_basis='ZEE5 release',
                    language_basis='available audio tracks',scenes=False,dialogue=False)
    except Exception as exc:
        return JSONResponse({'status':'unavailable','reason':type(exc).__name__},status_code=503)

@app.post('/api/search')
async def search(payload:SearchRequest,request:Request):
    query=payload.query.strip()
    if not query:
        raise HTTPException(422,'Query cannot be blank')
    return await request.app.state.service.search(query,payload.limit,payload.debug)
