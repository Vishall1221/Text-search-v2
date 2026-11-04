"""Short title requests must not be hijacked by exact character matches."""
from dataclasses import replace
from datetime import date
import pytest
from app.gazetteer import Catalog
from app.planner import plan_query
from tests.conftest import memory_catalog


def title_catalog():
    template=memory_catalog().assets['F1']
    assets=[
        replace(template,asset_key='P1',asset_id='P1',title='Parasakthi - Uncesored Version',
                title_norm='parasakthi uncesored version',characters=[],characters_norm=[]),
        replace(template,asset_key='P2',asset_id='P2',title='Parimala and Co',title_norm='parimala and co',
                characters=['Parasakthi'],characters_norm=['parasakthi']),
        replace(template,asset_key='P3',asset_id='P3',title='Inthi Ninna Preethiya',title_norm='inthi ninna preethiya',
                characters=['Parimala'],characters_norm=['parimala'])]
    return Catalog(assets,doc_total=100,lexicon={'child':30,'family':20,'parasakthi':1,'parimala':1})


@pytest.mark.parametrize('query,key',[
    ('parasakthi','P1'),('Parimala','P2'),('play parasakthi','P1'),
    ('Parimala movies','P2'),('Parasakthi Tamil movies','P1'),
    ('Parimala and Co','P2'),('Parasakthi - Uncesored Version','P1')])
def test_title_request_beats_character_collision(query,key):
    plan=plan_query(query,title_catalog(),date(2026,11,4))
    assert plan.mode=='title' and plan.allowed=={key}, (plan.mode,plan.allowed,plan.facets)
    assert not any(f.kind=='character' for f in plan.facets)


@pytest.mark.parametrize('query,key',[
    ('movies with character Parimala','P3'),('character Parasakthi','P2'),
    ('movies featuring Parimala','P3'),('movies starring Parasakthi','P2')])
def test_explicit_character_request_keeps_character_intent(query,key):
    plan=plan_query(query,title_catalog())
    assert plan.allowed=={key} and not any(f.kind=='title' for f in plan.facets)


def test_complete_title_wins_over_other_longer_titles():
    cat=title_catalog()
    exact=replace(cat.assets['P1'],asset_id='P4',asset_key='P4',title='Parimala',title_norm='parimala')
    cat=Catalog([*cat.assets.values(),exact],doc_total=100)
    assert plan_query('Parimala',cat).allowed=={'P4'}


def test_frequent_character_name_does_not_override_title_intent():
    cat=title_catalog()
    cat.lexicon['parimala']=50
    assert plan_query('Parimala',cat).allowed=={'P2'}


def test_multiple_title_prefixes_offer_only_matching_titles():
    cat=title_catalog()
    extra=replace(cat.assets['P1'],asset_id='P4',asset_key='P4',title='Parimala Returns',title_norm='parimala returns')
    cat=Catalog([*cat.assets.values(),extra],doc_total=100)
    plan=plan_query('Parimala',cat)
    assert plan.mode=='title' and plan.allowed=={'P2','P4'}


def test_common_genre_and_story_words_are_not_title_prefix_filters():
    cat=title_catalog()
    family=replace(cat.assets['P1'],asset_id='P4',asset_key='P4',title='Family Secrets',title_norm='family secrets')
    child=replace(cat.assets['P1'],asset_id='P5',asset_key='P5',title='Child of Tomorrow',title_norm='child of tomorrow')
    cat=Catalog([*cat.assets.values(),family,child],doc_total=100,lexicon={'child':30,'family':20})
    assert not any(f.kind=='title' for f in plan_query('family films',cat).facets)
    assert not any(f.kind=='title' for f in plan_query('a child is kidnapped',cat).facets)
    assert not any(f.kind=='title' for f in plan_query('a woman named Parimala rescues a child',cat).facets)
    assert plan_query('play Family',cat).allowed=={'P4'}


def test_fragments_use_contiguous_whole_words_anywhere_in_title():
    cat=title_catalog()
    extra=replace(cat.assets['P1'],asset_id='P4',asset_key='P4',title='Parimalam Returns',title_norm='parimalam returns')
    cat=Catalog([*cat.assets.values(),extra],doc_total=100)
    assert plan_query('Ninna Preethiya',cat).allowed=={'P3'}
    assert plan_query('Parimala',cat).allowed=={'P2'}
    assert not any(sp.match=='title_fragment' for sp in cat.find_spans(['preethiya','ninna']))


def test_title_lookup_uses_postings_without_scanning_assets():
    class NoAssetScan(dict):
        def __iter__(self):
            raise AssertionError('Title lookup must use its index')
        def items(self):
            raise AssertionError('Title lookup must use its index')
        def values(self):
            raise AssertionError('Title lookup must use its index')
    cat=title_catalog()
    cat.assets=NoAssetScan(cat.assets)
    spans=cat.find_spans(['parasakthi'])
    title=next(e for sp in spans for e in sp.entities if e.kind=='title')
    assert cat.assets_for(title)=={'P1'}
