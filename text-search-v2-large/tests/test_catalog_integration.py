"""Real PostgreSQL serving tests in a guarded disposable database."""
import asyncio
from contextlib import asynccontextmanager
import dataclasses
import json
import os

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest

from app.embedding import HashEmbedder
from app.service import SearchService
from catalog_large.ingest import ingest, rollback
from tests.conftest import sources

URL=os.getenv('LARGE_TEST_DATABASE_URL','')
pytestmark=[pytest.mark.integration,pytest.mark.skipif(not URL,reason='LARGE_TEST_DATABASE_URL not set')]

@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    assert 'test' in conninfo_to_dict(URL).get('dbname','')
    assert conninfo_to_dict(URL).get('dbname') != 'ai_search'
    import app.service as service
    import app.db as db
    import app.api as api
    cfg=dataclasses.replace(service.settings,database_url=URL,generation_check_seconds=0)
    monkeypatch.setattr(service,'settings',cfg)
    monkeypatch.setattr(db,'settings',cfg)
    monkeypatch.setattr(api,'settings',cfg)
    monkeypatch.setenv('LARGE_ALLOW_HASH_TEST','1')
    with psycopg.connect(URL,autocommit=True) as conn:
        conn.execute('DROP SCHEMA IF EXISTS schema_v2_large CASCADE')
    path=tmp_path/'movies.json'
    path.write_text(json.dumps(sources()),encoding='utf-8')
    report=ingest(path,url=URL,embedder=HashEmbedder())
    return path,report

@asynccontextmanager
async def service(encoder=None):
    svc=SearchService(encoder or HashEmbedder())
    try:
        await svc.start()
        yield svc
    finally:
        await svc.stop()

async def test_titles_aliases_and_audio_filters(database):
    async with service() as svc:
        for query,ids in [
            ('Thalapathy movies',{'F1'}),('Sallu Bhai films',{'F2'}),
            ('Tamil and Hindi movies',{'F1','F3','F6'}),('Tamil dubbed movies',{'F1','F3','F6'}),
            ('English or Telugu movies',{'F2','F4','F5'})]:
            response=await svc.search(query)
            assert {r['show_id'] for r in response['results']}==ids,response
            assert response['status']=='ok'
            assert all('primary_language' not in r and 'scenes' not in r for r in response['results'])

async def test_synonyms_typo_recovery_and_no_scene_queries(database,monkeypatch):
    async with service() as svc:
        recorded=[]
        real=svc._fetch
        async def recording(sql,params,timeout):
            recorded.append(sql)
            return await real(sql,params,timeout)
        monkeypatch.setattr(svc,'_fetch',recording)
        for query in ['child abducted','child kidnpped']:
            response=await svc.search(query,methods={'lex'},debug=True)
            assert response['results'][0]['show_id']=='F3',response
        mood=await svc.search('feel-good movies',methods={'lex'})
        assert mood['results'][0]['show_id']=='F1' and mood['status']=='approximate',mood
        for query in ['scenes where a child is kidnapped','he says I will return']:
            assert (await svc.search(query))['status']=='needs_clarification'
        assert all('search_v2.' not in sql and 'scene_snapshot' not in sql for sql in recorded)

async def test_platform_dates_similar_and_negative_lexical(database):
    async with service() as svc:
        result=await svc.search('latest Tamil movies after 2021')
        assert [r['show_id'] for r in result['results']]==['F1','F3']
        result=await svc.search('movies like Sunrise Promise')
        assert 'F1' not in {r['show_id'] for r in result['results']}
        assert result['results']
        result=await svc.search('quantum submarine treaty negotiations',methods={'lex'})
        assert result['status']=='empty'

async def test_generation_switch_and_rollback_refresh_aliases(database):
    path,first=database
    async with service() as svc:
        assert (await svc.search('Thalapathy movies'))['results']
        changed=sources()[1:]
        path.write_text(json.dumps(changed))
        second=await asyncio.to_thread(ingest,path,url=URL,embedder=HashEmbedder())
        response=await svc.search('Sunrise Promise')
        assert response['generation']==second['generation_id']
        assert 'F1' not in {r['show_id'] for r in response['results']}
        with psycopg.connect(URL) as conn:
            rollback(conn)
        response=await svc.search('Thalapathy movies')
        assert response['generation']==first['generation_id']
        assert [r['show_id'] for r in response['results']]==['F1']

async def test_model_mismatch_falls_back_with_degraded_status(database):
    encoder=HashEmbedder()
    encoder.profile=dataclasses.replace(encoder.profile,name='another-model')
    async with service(encoder) as svc:
        result=await svc.search('child kidnapped')
        assert result['status']=='degraded' and result['results'][0]['show_id']=='F3'
        assert 'dense' in result['degraded']

async def test_timeout_and_cancellation_leave_pool_usable(database,monkeypatch):
    async with service() as svc:
        slow=asyncio.create_task(svc._fetch('SELECT pg_sleep(3)',{},5000))
        await asyncio.sleep(.05)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        assert (await svc.search('Sunrise Promise'))['results'][0]['show_id']=='F1'
        with pytest.raises(psycopg.errors.QueryCanceled):
            await svc._fetch('SELECT pg_sleep(1)',{},30)
        assert (await svc._fetch('SELECT 1 AS ok',{},500))[0]['ok']==1

async def test_api_metadata_contract_and_invalid_inputs(database,monkeypatch):
    import app.api as api
    monkeypatch.setattr(api,'load_embedder',lambda:HashEmbedder())
    async with api.app.router.lifespan_context(api.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
            ready=await client.get('/api/ready')
            assert ready.status_code==200 and ready.json()['schema']=='schema_v2_large'
            assert ready.json()['assets']==6 and ready.json()['dense']
            response=await client.post('/api/search',json={'query':'Thalapthy movies'})
            assert response.status_code==200 and response.json()['results'][0]['show_id']=='F1'
            assert (await client.get('/api/video/F1')).status_code==404
            for payload in [{'query':' '},{'query':'x'*501},{'query':'movies','limit':0}]:
                assert (await client.post('/api/search',json=payload)).status_code==422
            assert (await client.get('/')).status_code==200
