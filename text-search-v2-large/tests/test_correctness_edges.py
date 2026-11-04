"""Independent adversarial cases using names outside the reported examples."""
from dataclasses import replace

import pytest

from app.gazetteer import Catalog
from app.planner import plan_query
from app.text import normalize
from tests.conftest import memory_catalog


@pytest.fixture
def edge_catalog():
    base=memory_catalog().assets['F1']
    specs=[
        ('E1','Neruppu - Restored Edition',['Arun Kumar'],['Ravi'],['Shankar']),
        ('E2','Arun Kumar',['Bala'],['Arun Kumar','Silver Crown'],['Other Director']),
        ('E3','Karuppan and Sons',['Chandra'],['Commander'],['Arun Kumar']),
        ('E4','3',[],[],[]),('E5','96',[],[],[]),
        ('E6','நிழல் வரிகள்',[],[],[]),('E7','ஒரு பாதை',[],[],[]),
        ('E8','Nadipathi',[],[],[]),
    ]
    assets=[replace(base,asset_key=key,asset_id=key,title=title,title_norm=normalize(title),
        cast_names=cast,cast_norm=list(map(normalize,cast)),characters=chars,
        characters_norm=list(map(normalize,chars)),directors=directors,
        genres=[],genres_norm=[],themes=[]) for key,title,cast,chars,directors in specs]
    aliases=[dict(alias_norm=alias,actor_name=name,actor_norm=normalize(name),requires_context=guard)
        for alias,name,guard in [('nadipathi','Arun Kumar',False),('thunder king','Arun Kumar',True),
            ('silver crown','Arun Kumar',False),('silver crown','Bala',False),
            ('maravan','Arun Kumar',False),('marivan','Bala',False)]]
    return Catalog(assets,doc_total=100,actor_aliases=aliases,
        lexicon={'the':80,'child':30,'a':50,'saves':20,'village':20})


@pytest.mark.parametrize('query,key',[
    ('Arun Kumar movies','E1'),('play Arun Kumar','E2'),
    ('movie called Arun Kumar','E2'),('actor named Arun Kumar movies','E1'),
    ('movies with a character named Arun Kumar','E2'),
    ('movies directed by Arun Kumar','E3'),('director named Arun Kumar movies','E3'),
    ('Karuppin','E3'),('movie named Karuppan','E3'),('movie called Karuppin','E3'),
    ('3','E4'),('96 movies','E5'),
    ('movie called 3','E4'),('Tamil 96 movies','E5'),
    ('நிழல் வரிகள்','E6'),('நிழல்','E6'),('play ஒரு பாதை','E7'),
    ('Nadipathi movies','E1'),('play Nadipathi','E8'),
    ('actor named Thunder King','E1'),('starring Thunder King','E1'),
    ('starring Marivanx','E2'),('character named Arunn Kumar','E2'),
    ('director named Arunn Kumar','E3')])
def test_role_title_language_and_alias_edges(edge_catalog,query,key):
    p=plan_query(query,edge_catalog)
    assert p.clarification is None,(query,p.clarification)
    assert p.allowed=={key},(query,p.mode,p.allowed,p.facets)


@pytest.mark.parametrize('query',[
    'starring Shankar','directed by Commander','actor named Commander',
    'character named Shankar','movie called Shankar'])
def test_explicit_wrong_role_is_never_satisfied_by_other_relationship(edge_catalog,query):
    p=plan_query(query,edge_catalog)
    assert p.clarification is not None or p.allowed==set(),(query,p.mode,p.allowed,p.facets)


@pytest.mark.parametrize('query',['Silver Crown movies','actor named Silver Crown','Marevan movies'])
def test_competing_alias_targets_do_not_pick_an_arbitrary_actor(edge_catalog,query):
    p=plan_query(query,edge_catalog)
    assert p.clarification is not None,(query,p.mode,p.allowed,p.facets)


@pytest.mark.parametrize('query',[
    'a thunder king saves a village','a marevan saves a child in the village',
    'a marivanx and a child escape','a man called karuppin saves a village'])
def test_narrative_words_do_not_gain_name_corrections(edge_catalog,query):
    p=plan_query(query,edge_catalog)
    assert not p.corrections,(query,p.corrections)
    assert not any(f.kind=='actor' for f in p.facets),(query,p.facets)


def test_actor_negation_does_not_exclude_character_only_film(edge_catalog):
    p=plan_query('movies without actor Arun Kumar',edge_catalog)
    assert p.allowed==set(edge_catalog.assets)-{'E1'},(p.allowed,p.facets,p.clarification)
