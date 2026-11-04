"""Real FTS stage-tier ordering in the guarded disposable test database."""
import asyncio
from copy import deepcopy
import json
import pytest

from catalog_large.ingest import ingest
from tests.conftest import sources
from tests.test_catalog_integration import URL, database, service

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not URL,reason='LARGE_TEST_DATABASE_URL not set')]


async def test_postgres_relaxation_preserves_all_term_match(database):
    path,_=database
    records=[]
    for key,title,plot in [
        ('strict','Zebra Case','A detective recovers a diamond. '+'A quiet village remembers its history. '*30),
        ('weak','Alpha Case','A detective detective detective detective investigates.'),
        ('weak2','Beta Case','A diamond diamond diamond diamond disappears.')]:
        row=deepcopy(sources()[0])
        row.update(show_id=key,title=title,plot_summary=plot,themes=[],genre='Drama')
        records.append(row)
    path.write_text(json.dumps(records),encoding='utf-8')
    await asyncio.to_thread(ingest,path,url=URL,lexical_only=True)
    async with service() as svc:
        result=await svc.search('detective diamond',methods={'lex'})
        ids=[row['show_id'] for row in result['results']]
        assert ids[0]=='strict' and set(ids)=={'strict','weak','weak2'},result
