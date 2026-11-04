"""Uncalibrated semantic neighbors alone cannot justify ambiguous short lookups."""
import pytest
from app.fusion import AssetResult
from app.planner import Plan, Facet
from app.gazetteer import Entity
from app.service import SearchService, Deadline
from tests.conftest import memory_catalog


@pytest.mark.parametrize('text,methods,filtered,lexical,expected',[
    ('high',None,False,False,'needs_clarification'),
    ('quuxnonsense',None,False,False,'needs_clarification'),
    ('a father rescues his child',None,False,False,'approximate'),
    ('heartwarming hopeful uplifting',None,False,False,'approximate'),
    ('high',{'dense'},False,False,'approximate'),
    ('high',None,True,False,'approximate'),
    ('high',None,False,True,'ok'),
])
async def test_short_query_requires_evidence_but_descriptive_discovery_survives(
        monkeypatch,text,methods,filtered,lexical,expected):
    service=SearchService(None)
    plan=Plan(raw=text,scrubbed=text,normalized=text,mode='catalog',terms=text.split(),dense_text=text)
    if filtered:
        plan.facets=[Facet([Entity('actor','Vijay','vijay')])]
        plan.allowed={'F1'}
    async def fetch(*args):
        return [dict(asset_key='F1',score=.1)] if lexical else []
    async def embed(*args): return None
    async def dense(*args): return [AssetResult('F1')]
    monkeypatch.setattr(service,'_fetch',fetch)
    monkeypatch.setattr(service,'_dense_reason',lambda cat:'')
    monkeypatch.setattr(service,'_timed_embed',embed)
    monkeypatch.setattr(service,'_dense_hits',dense)
    try:
        response=await service._execute(plan,memory_catalog(),10,Deadline(950),True,(None,methods))
        assert response['status']==expected,response
        assert bool(response['results']) == (expected!='needs_clarification')
        if expected=='needs_clarification':
            assert response['clarification']['reason']=='unsupported_short_query'
    finally:
        await service.stop()
