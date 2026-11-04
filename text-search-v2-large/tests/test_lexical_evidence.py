"""Relaxed lexical recall supplements strict evidence rather than replacing it."""
import pytest

from app.planner import Plan
from app.service import Deadline, SearchService
from tests.conftest import memory_catalog


@pytest.mark.parametrize('terms,responses,expected',[
    (['detective','diamond'],
     [[('F2',.01)],[('F1',.9),('F2',.8),('F3',.7)]],['F2','F1','F3']),
    (['detective','diamond','betrayal','heist'],
     [[('F2',.01)],[('F3',.9),('F2',.8)],[('F1',.99),('F3',.9)]],['F2','F3','F1']),
    (['detective','diamond'],
     [[],[('F1',.9),('F2',.8)]],['F1','F2'])])
async def test_strong_stage_order_is_retained_and_relaxed_results_deduplicated(terms,responses,expected,monkeypatch):
    svc=SearchService(None)
    calls=[]
    async def fetch(sql,params,timeout):
        calls.append(params['q'])
        return [dict(asset_key=key,score=score) for key,score in responses[len(calls)-1]]
    monkeypatch.setattr(svc,'_fetch',fetch)
    plan=Plan(raw=' '.join(terms),scrubbed=' '.join(terms),normalized=' '.join(terms),
              terms=terms,mode='catalog')
    try:
        hits=await svc._retrieve(plan,memory_catalog(),Deadline(950),{},(None,{'lex'}))
        assert [hit.asset_key for hit in hits]==expected
        assert len(calls)==len(responses)
    finally:
        await svc.stop()


async def test_sufficient_strict_evidence_does_not_query_weaker_stages(monkeypatch):
    svc=SearchService(None)
    calls=[]
    async def fetch(sql,params,timeout):
        calls.append(params['q'])
        return [dict(asset_key=f'F{i}',score=.1) for i in range(1,7)]
    monkeypatch.setattr(svc,'_fetch',fetch)
    plan=Plan(raw='detective diamond',scrubbed='detective diamond',normalized='detective diamond',
              terms=['detective','diamond'],mode='catalog')
    try:
        hits=await svc._retrieve(plan,memory_catalog(),Deadline(950),{},(None,{'lex'}))
        assert len(hits)==6 and len(calls)==1
    finally:
        await svc.stop()
