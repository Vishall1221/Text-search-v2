"""End-to-end synthetic relevance evaluation through ASGI, PostgreSQL and real BGE.

Requires LARGE_DATABASE_URL naming a disposable database containing 'test'.
python -m scripts.evaluate_catalog_api --ingest --negative-controls --output runtime/relevance/api-after.json
python -m scripts.evaluate_catalog_api --output runtime/relevance/api-after.json --compare runtime/relevance/api-before.json

--ingest replaces only schema_v2_large's active snapshot in that test database.
--negative-controls also imports a Highway-only snapshot, tests no-evidence queries,
and restores the main synthetic snapshot. Original database namespaces are untouched.
The 63 fixed judgments are synthetic. Narrative cases check interpretation safety,
not semantic relevance; no human-labelled semantic relevance claim is made.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

import httpx
from psycopg.conninfo import conninfo_to_dict

from app.runtime import configure_event_loop
from app.embedding import load_embedder
from catalog_large.ingest import ingest
from catalog_large.source import DEFAULT_ALIASES
from config.settings import settings
from tests.relevance_cases import CASES,records


def percentile(values,p):
    values=sorted(values)
    return round(values[int(p*(len(values)-1))],3) if values else None


def summary(cases):
    ranked=[case for case in cases if case['expected'] is not None]
    tp=fp=fn=0
    for case in ranked:
        expected=set(case['expected'])
        actual=set(case['result_ids'])
        tp+=len(expected&actual)
        fp+=len(actual-expected)
        fn+=len(expected-actual)
    return dict(passed=sum(case['passed'] for case in cases),total=len(cases),
                finite_set_queries=len(ranked),true_positive=tp,false_positive=fp,false_negative=fn,
                micro_precision=round(tp/max(tp+fp,1),4),micro_recall=round(tp/max(tp+fn,1),4),
                reciprocal_rank=round(sum(1/case['first_relevant_rank'] if case['first_relevant_rank'] else 0
                                          for case in ranked)/max(len(ranked),1),4),
                scope='finite-set relevance only for labelled title/person queries; narrative cases check filter safety')


async def search_cases(cases):
    import app.api as api
    outcomes=[]
    async with api.app.router.lifespan_context(api.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://evaluation') as client:
            ready_response=await client.get('/api/ready')
            ready=ready_response.json()
            assert ready_response.status_code==200 and ready.get('dense'),ready
            model=api.app.state.service.embedder
            assert model is not None and model.profile.name!='hash-test','A real embedding model is required'
            model_identity=dict(name=model.profile.name,revision=model.revision,pooling=model.profile.pooling,
                                query_prefix=model.profile.query_prefix,dimensions=model.profile.dim)
            for case in cases:
                started=time.perf_counter()
                response=await client.post('/api/search',json={'query':case['query'],'limit':50,'debug':True})
                wall_ms=(time.perf_counter()-started)*1000
                body=response.json()
                ids=[row['show_id'] for row in body.get('results',[])]
                filters=body.get('interpretation',{}).get('filters',[])
                kinds=sorted({label.split(':',1)[0].removeprefix('NOT ') for label in filters})
                expected=set(case['expected'] or [])
                failures=[]
                if response.status_code!=200: failures.append('http')
                if body.get('status')=='degraded': failures.append('degraded')
                if case['expected'] is not None and set(ids)!=expected: failures.append('results')
                if case['clarify'] != 'optional' and (body.get('status')=='needs_clarification') != case['clarify']:
                    failures.append('clarification')
                if set(kinds)&set(case['forbidden']): failures.append('forbidden_facet')
                if case['kinds'] is not None and sorted(k for k in kinds if k!='content_type')!=sorted(case['kinds']):
                    failures.append('entity_role')
                first=next((rank for rank,key in enumerate(ids,1) if key in expected),None) if expected else None
                outcomes.append(dict(**case,passed=not failures,failures=failures,result_ids=ids,
                    result_titles=[row['title'] for row in body.get('results',[])],
                    first_relevant_rank=first,http_status=response.status_code,status=body.get('status'),
                    interpretation=body.get('interpretation'),clarification=body.get('clarification'),
                    notes=body.get('notes'),debug=body.get('debug'),timings_ms=body.get('timings_ms'),
                    wall_ms=round(wall_ms,3),generation=body.get('generation')))
    return outcomes,ready,model_identity


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--compare',type=Path)
    parser.add_argument('--ingest',action='store_true')
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args()
    assert os.getenv('LARGE_DATABASE_URL'),'Set LARGE_DATABASE_URL explicitly to a disposable test database'
    dbname=conninfo_to_dict(settings.database_url).get('dbname','')
    assert 'test' in dbname.casefold() and dbname!='ai_search','Refusing non-test database'
    assert settings.dense_enabled,'Real dense retrieval must be enabled for this evaluation'
    assert not args.negative_controls or args.ingest,'--negative-controls requires --ingest to restore the fixture'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    data=args.output.parent/'api-fixture-movies.json'
    aliases=args.output.parent/'api-fixture-aliases.json'
    data.write_text(json.dumps(records(),ensure_ascii=False),encoding='utf-8')
    groups=json.loads(DEFAULT_ALIASES.read_text(encoding='utf-8-sig'))
    groups['actors'] += [dict(catalog_names=[target],aliases=[alias],provenance='synthetic_fixture')
                         for alias,target in [('Nova','Nila Menon'),('The Navigator','Dev Patel')]]
    aliases.write_text(json.dumps(groups,ensure_ascii=False),encoding='utf-8')
    encoder=load_embedder() if args.ingest else None
    imported=ingest(data,url=settings.database_url,aliases_file=aliases,embedder=encoder) if args.ingest else None
    configure_event_loop()
    outcomes,ready,identity=asyncio.run(search_cases(CASES))
    report=dict(synthetic=True,catalog_facts_verified=False,fixture_movies=len(records()),
                scope='Actual ASGI API, real PostgreSQL FTS and BGE; synthetic labelled catalog, unavailable Mac export',
                judgments_sha256=hashlib.sha256(json.dumps(CASES,sort_keys=True).encode()).hexdigest(),
                fixture_sha256=hashlib.sha256(json.dumps(records(),sort_keys=True).encode()).hexdigest(),
                model_identity=identity,readiness=ready,import_report=imported,summary=summary(outcomes),
                latency=dict(requests=len(outcomes),p50_ms=percentile([x['wall_ms'] for x in outcomes],.5),
                             p95_ms=percentile([x['wall_ms'] for x in outcomes],.95),
                             max_ms=round(max(x['wall_ms'] for x in outcomes),3),
                             scope='serial ASGI requests; query embeddings initially uncached; excludes process/model startup'),
                cases=outcomes)
    if args.negative_controls:
        negative_data=args.output.parent/'api-negative-movies.json'
        negative_data.write_text(json.dumps([row for row in records() if row['show_id']=='M08']),encoding='utf-8')
        control_cases=[dict(query=query,expected=[],group='negative_control',mode=None,kinds=None,
                            forbidden=['title'],clarify='optional') for query in ['quuxnonsense','high']]
        try:
            ingest(negative_data,url=settings.database_url,aliases_file=aliases,embedder=encoder)
            controls,control_ready,_=asyncio.run(search_cases(control_cases))
            report['negative_controls']=dict(scope='Only Highway exists; neither query has word-level evidence',
                                              readiness=control_ready,summary=summary(controls),cases=controls)
        finally:
            report['restored_import']=ingest(data,url=settings.database_url,aliases_file=aliases,embedder=encoder)
    if args.compare:
        before=json.loads(args.compare.read_text(encoding='utf-8'))
        assert before['judgments_sha256']==report['judgments_sha256'],'Judgments changed; comparison invalid'
        assert before['fixture_sha256']==report['fixture_sha256'],'Catalog fixture changed; comparison invalid'
        assert before['model_identity']==report['model_identity'],'Embedding model changed; comparison invalid'
        previous={case['query']:case for case in before['cases']}
        report['comparison']=dict(before_summary=before['summary'],
            improved=[case['query'] for case in outcomes if case['passed'] and not previous[case['query']]['passed']],
            regressed=[case['query'] for case in outcomes if not case['passed'] and previous[case['query']]['passed']],
            still_failed=[case['query'] for case in outcomes if not case['passed'] and not previous[case['query']]['passed']])
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ['synthetic','model_identity','summary','latency']},indent=2))
    if 'comparison' in report: print(json.dumps(report['comparison'],indent=2))
    for case in outcomes:
        if not case['passed']: print('FAIL',case['query'],case['failures'],case['result_ids'])


if __name__=='__main__': main()
