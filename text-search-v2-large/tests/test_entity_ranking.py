"""Relationship evidence orders listings; explicit date order remains authoritative."""
from copy import deepcopy

from app.gazetteer import Catalog, Entity, asset_from_row
from app.planner import Facet, Plan
from app.service import Deadline, SearchService
from catalog_large.source import normalize_movie
from tests.conftest import sources


def listing_catalog():
    template=sources()[0]
    rows=[]
    for key,title,lead,release in [('lead','Zebra Promise',True,'2020-01-01'),
                                  ('support','Alpha Guest',False,'2025-01-01'),
                                  ('other','Beta Story',True,'2022-01-01')]:
        row=deepcopy(template)
        row.update(show_id=key,title=title,released_date=release,
                   primary_cast=[{'actor_name':'Mira Rao' if lead else 'Another Person','character_name':None}],
                   supporting_cast=[] if lead else [{'actor_name':'Mira Rao','character_name':None}])
        rows.append(normalize_movie(row,'fixture'))
    cat=Catalog([asset_from_row(row) for row in rows],doc_total=30)
    cat.metadata={row['show_id']:row for row in rows}
    return cat


async def execute(plan,cat):
    svc=SearchService(None)
    try:
        result=await svc._execute(plan,cat,10,Deadline(950),True)
        return [row['show_id'] for row in result['results']]
    finally:
        await svc.stop()


async def test_actor_leads_precede_supporting_appearances_without_excluding_them():
    plan=Plan(raw='Mira Rao movies',scrubbed='Mira Rao movies',normalized='mira rao movies',mode='browse',
              facets=[Facet([Entity('actor','Mira Rao','mira rao')],surface='Mira Rao')],
              allowed={'lead','support','other'})
    assert await execute(plan,listing_catalog())==['other','lead','support']


async def test_explicit_latest_sort_overrides_primary_cast_order():
    plan=Plan(raw='latest Mira Rao movies',scrubbed='latest Mira Rao movies',normalized='latest mira rao movies',
              mode='browse',sort='latest',
              facets=[Facet([Entity('actor','Mira Rao','mira rao')],surface='Mira Rao')],
              allowed={'lead','support','other'})
    assert await execute(plan,listing_catalog())==['support','other','lead']


async def test_unconstrained_listings_keep_stable_title_order():
    plan=Plan(raw='movies',scrubbed='movies',normalized='movies',mode='browse')
    assert await execute(plan,listing_catalog())==['support','other','lead']


async def test_title_fragments_prefer_title_start_then_less_unmatched_wording():
    rows=[]
    for key,title in [('middle','A Moonlit Tale'),('long','Moonlit Shadows Return'),('short','Moonlit Letters')]:
        row=deepcopy(sources()[0])
        row.update(show_id=key,title=title)
        rows.append(normalize_movie(row,'fixture'))
    cat=Catalog([asset_from_row(row) for row in rows],doc_total=30)
    cat.metadata={row['show_id']:row for row in rows}
    plan=Plan(raw='Moonlit',scrubbed='Moonlit',normalized='moonlit',mode='title',
              facets=[Facet([Entity('title',row['title'],row['title_norm']) for row in rows],surface='Moonlit')],
              allowed={'middle','long','short'})
    assert await execute(plan,cat)==['short','long','middle']


async def test_negated_actor_does_not_promote_other_lead_appearances():
    plan=Plan(raw='movies without Mira Rao',scrubbed='movies without Mira Rao',normalized='movies without mira rao',
              mode='browse',facets=[Facet([Entity('actor','Mira Rao','mira rao')],True,'Mira Rao')],
              allowed={'support','other','lead'})
    assert await execute(plan,listing_catalog())==['support','other','lead']
