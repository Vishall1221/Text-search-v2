import asyncio
from dataclasses import replace
from types import MethodType
import threading
import time
import numpy as np
import pytest
from app.service import SearchService
from app.planner import plan_query
from tests.conftest import memory_catalog

async def test_cancelled_parent_cleans_its_children(monkeypatch):
    svc=SearchService(None)
    cat=memory_catalog()
    svc._catalog=cat
    outcomes=[]
    async def catalog(): return cat
    async def slow(*args):
        try:
            await asyncio.sleep(1)
        finally:
            outcomes.append('closed')
    monkeypatch.setattr(svc,'catalog',catalog)
    monkeypatch.setattr(svc,'_fetch',slow)
    task=asyncio.create_task(svc.search('a merchant rescues a child',methods={'lex'}))
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert outcomes==['closed']
    await svc.stop()

async def test_deadline_returns_degraded_partial_response(monkeypatch):
    import app.service as module
    monkeypatch.setattr(module,'settings',replace(module.settings,request_deadline_ms=20))
    svc=SearchService(None)
    async def catalog():return memory_catalog()
    async def slow(*args):await asyncio.sleep(1);return []
    monkeypatch.setattr(svc,'catalog',catalog)
    monkeypatch.setattr(svc,'_fetch',slow)
    start=time.perf_counter()
    result=await svc.search('merchant rescues child',methods={'lex'})
    assert result['status']=='degraded' and time.perf_counter()-start<.2
    await svc.stop()

async def test_cancelled_inference_holds_admission_until_thread_finishes(monkeypatch):
    import app.service as module
    monkeypatch.setattr(module,'settings',replace(module.settings,embed_concurrency=1))
    started,release,next_started=threading.Event(),threading.Event(),threading.Event()
    class Encoder:
        def encode_query(self,text):
            if text=='first':
                started.set();release.wait(2)
            else:next_started.set()
            return np.zeros(384,dtype=np.float32)
    svc=SearchService(Encoder())
    first=asyncio.create_task(svc._embed('first'))
    try:
        while not started.is_set():await asyncio.sleep(.001)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        second=asyncio.create_task(svc._embed('second'))
        await asyncio.sleep(.02)
        assert not next_started.is_set()
        second.cancel()
        await asyncio.gather(second,return_exceptions=True)
    finally:
        release.set()
        await asyncio.sleep(.03)
        await svc.stop()

def test_stale_calibration_does_not_filter_another_corpus(monkeypatch):
    import app.service as module
    cat=memory_catalog()
    cat.generation.update(embedding_model='BGE',model_revision='rev',pooling='cls',query_prefix='',text_version='large-catalog-1',passage_tokens=384,notes={'corpus_sha256':'new'})
    calibration=dict(model='BGE',revision='rev',pooling='cls',query_prefix='',text_version='large-catalog-1',passage_tokens=384,corpus_sha256='old',dense_min_similarity=.9)
    monkeypatch.setattr(module,'settings',replace(module.settings,calibration=calibration,dense_threshold_explicit=False))
    svc=SearchService(None)
    assert svc.threshold_for(cat) is None
    svc._executor.shutdown(wait=False)
