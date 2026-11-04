"""Synthetic-only integration benchmark. Refuses non-test databases."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import tracemalloc
import numpy as np
from psycopg.conninfo import conninfo_to_dict
from catalog_large.ingest import ingest
from app.embedding import load_embedder
from app.runtime import run_async
from app.service import SearchService
from config.settings import settings
from tests.conftest import sources

QUERIES=['Sunrise Promise','Thalapathy movies','Vaigai Puyal movies','Sallu Bhai films',
 'Tamil and Hindi movies','English or Telugu movies','family films','feel-good movies',
 'child abducted','child kidnpped','movies like Sunrise Promise','latest Tamil movies after 2021',
 'a detective seeks vengeance','heartwarming movies','spooky movies']

async def measure(encoder,out):
    tracemalloc.start()
    svc=SearchService(encoder)
    start=time.perf_counter()
    await svc.start()
    startup=(time.perf_counter()-start)*1000
    current,peak=tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result=dict(catalog_load_ms=round(startup,2),python_snapshot_current_mb=round(current/1024**2,2),
                python_snapshot_peak_mb=round(peak/1024**2,2),memory_scope='Python snapshot allocations, excludes native ONNX/PG memory')
    try:
        checks={}
        for query,expected in [('Sunrise Promise','F1'),('Thalapathy movies','F1'),('Sallu Bhai films','F2'),('child abducted','F3')]:
            response=await svc.search(query)
            checks[query]=response['results'][0]['show_id']==expected if response['results'] else False
        result['fixture_top1_checks']=checks
        for query in QUERIES:
            await svc.search(query)
        for concurrency in (1,4,8):
            sem=asyncio.Semaphore(concurrency)
            async def one(i):
                async with sem:
                    start=time.perf_counter()
                    response=await svc.search(QUERIES[i%len(QUERIES)])
                    return (time.perf_counter()-start)*1000,response['status']
            responses=await asyncio.gather(*(one(i) for i in range(200)))
            ms=[r[0] for r in responses]
            result[f'clients_{concurrency}']=dict(requests=len(ms),p50_ms=round(float(np.percentile(ms,50)),2),
                p95_ms=round(float(np.percentile(ms,95)),2),max_ms=round(max(ms),2),
                degraded=sum(r[1]=='degraded' for r in responses))
    finally:
        await svc.stop()
    out.write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--assets',type=int,default=2357)
    args=p.parse_args()
    assert 'test' in conninfo_to_dict(settings.database_url).get('dbname',''), 'Use a disposable test DB'
    out=Path('runtime/verification')
    out.mkdir(parents=True,exist_ok=True)
    rows=sources()
    for i in range(max(args.assets-len(rows),0)):
        rows.append(dict(show_id=f'X{i:05}',title=f'Synthetic Catalog Item {i:05}',content_type='movie',
            plot_summary='A merchant travels across a coastal town and solves a local dispute with courage.',
            themes=['Travel','Courage'],genre=['Action','Comedy','Drama'][i%3],
            primary_cast=[dict(actor_name=f'Fixture Actor {i:05}',character_name=None)],supporting_cast=[],
            audio_languages=[['Tamil','Hindi'],['English'],['Hindi','Telugu']][i%3],
            released_date=f'{2010+i%15}-01-01',director_name=None))
    corpus=out/'synthetic_movies.json'
    corpus.write_text(json.dumps(rows),encoding='utf-8')
    encoder=load_embedder()
    start=time.perf_counter()
    report=ingest(corpus,embedder=encoder)
    report['wall_seconds']=round(time.perf_counter()-start,2)
    (out/'import.json').write_text(json.dumps(report,indent=2,default=str))
    (out/'load_queries.json').write_text(json.dumps([{'text':q} for q in QUERIES]))
    print(json.dumps(report,indent=2,default=str),flush=True)
    run_async(measure(encoder,out/'benchmark.json'))

if __name__=='__main__':
    main()
