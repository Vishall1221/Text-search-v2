from datetime import date
import pytest
from app.runtime import configure_event_loop
from app.gazetteer import Catalog, asset_from_row
from catalog_large.source import normalize_movie, build_aliases

configure_event_loop()

def sources():
    values=[
      ('F1','Sunrise Promise','Family Drama',['Hope','Friendship'],['Vijay','Vadivelu'],['Tamil','Hindi'],'2024-01-01',
       'A heartwarming family is reunited. Friends find happiness and hope after reconciliation.'),
      ('F2','Docks of Revenge','Action',['Revenge','Betrayal'],['Salman Khan'],['Hindi','Telugu'],'2023-01-01',
       'A betrayed detective seeks vengeance against a smuggler planning a diamond heist.'),
      ('F3','Lost Child','Thriller',['Abduction'],['Suriya','Nayanthara'],['Tamil','Hindi'],'2022-01-01',
       'A child is kidnapped. A father protects his daughter and rescues the child.'),
      ('F4','Silent Manor','Horror',['Mystery'],['Alice Smith'],['English','Hindi'],'2021-01-01',
       'A ghost haunts a manor. An investigator solves an ancient mystery.'),
      ('F5','The Gift','Drama',['Loss'],['Bob Smith'],['English'],'2020-01-01',
       'An old man finds a gift and returns a lost letter.'),
      ('F6','Moonlit Letters','Romantic Drama',['Love'],['Shah Rukh Khan'],['Tamil','Hindi'],'2019-01-01',
       'Two lovers exchange letters across a border and overcome separation.')]
    return [dict(show_id=k,title=t,content_type='movie',genre=g,themes=themes,
        primary_cast=[dict(actor_name=n,character_name='Character '+n) for n in actors],
        supporting_cast=[],director_name=['Director One'] if k=='F1' else None,
        audio_languages=langs,released_date=d,plot_summary=plot) for k,t,g,themes,actors,langs,d,plot in values]

def memory_catalog():
    rows=[normalize_movie(r,'fixture') for r in sources()]
    aliases,_=build_aliases(rows)
    lexicon={w.strip('.,').lower():1 for row in rows for w in row['plot_summary'].split()}
    lexicon.update({'the':20,'child':3,'kidnapped':1,'gift':3})
    gen=dict(generation_id=1,doc_count=40,asset_count=len(rows))
    cat=Catalog([asset_from_row(r) for r in rows],lexicon=lexicon,generation=gen,doc_total=40,actor_aliases=aliases)
    cat.metadata={r['show_id']:r for r in rows}
    return cat

@pytest.fixture
def catalog():
    return memory_catalog()
