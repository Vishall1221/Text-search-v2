"""Hand-labelled synthetic relevance set; no claims about the unavailable Mac export.

Named examples reproduce metadata relationships from CHATGPT.md, not verified film
facts. Unseen examples deliberately include relationship and spelling collisions.
Keep judgments fixed between before/after runs; application code must not import this.
"""
from collections import Counter
from app.gazetteer import Catalog, asset_from_row
from app.text import normalize
from catalog_large.source import normalize_movie, build_aliases


def records():
    def movie(key, title, actors, characters=(), director='Arun Das', supporting=(),
              genre='Drama', languages=('Tamil',), plot='An unlikely friendship changes a village.'):
        return dict(show_id=key, title=title, content_type='movie', genre=genre,
                    primary_cast=[dict(actor_name=a,character_name=c) for a,c in
                                  zip(actors, list(characters)+[None]*len(actors))],
                    supporting_cast=[dict(actor_name=a,character_name=None) for a in supporting],
                    director_name=director, audio_languages=list(languages), themes=[],
                    released_date='2024-01-01', plot_summary=plot)
    return [
        movie('M01','Parasakthi – Uncensored Version',['Sivakumar']),
        movie('M02','Parimala and Co',['Asha Menon'],['Parasakthi']),
        movie('M03','Bhairava',['Vijay'],['Bhairava'],director='Shankar',genre='Action'),
        movie('M04','OP 2',['Arjun Rao'],['Inspector'],supporting=['Vijay']),
        movie('M05','Abhinethri',['Prabhu Deva'],['Vijay']),
        movie('M06','Veerame Vaigai Soodum',['Vishal'],['Thalapathy']),
        movie('M07','Inthi Ninna Preethiya',['Ramesh'],['Parimala']),
        movie('M08','Highway',['Ravi Kumar'],['Traveler']),
        movie('M09','2',['Maya Sen'],['Two']),
        movie('M10','Ka',['Dev Kumar'],['Ka']),
        movie('M11','Moonlit Letters',['Salman Khan'],['Ravi'],languages=('Hindi',)),
        movie('M12','River of Courage',['Suriya'],['Salman Khan']),
        movie('M13','Silent Harbour',['Mohan Lal'],['Shankar'],director='Deepa Nair'),
        movie('M14','Winter Lantern',['Shankar'],['Mohan'],director='Leela Rao'),
        movie('M15','Distant Shores',['Alice Rao'],['Sallu Bhai'],languages=('Hindi',)),
        movie('M16','Copper Horizon',['Dev Patel'],['Arjun'],director='Leela Rao'),
        movie('M17','Amber Station',['Nina Shah'],['Dev Patel'],director='Farah Ali'),
        movie('M18','Silver Orchard',['Leela Rao'],['Farah'],director='Arjun Rao'),
        movie('M19','Moonlit Roads',['Kiran Bose'],director='Leela Rao'),
        movie('M20','The Gift',['Anita Rao'],plot='An old man finds a gift and returns a lost letter.'),
        movie('M21','Family Secrets',['Rita Bose'],genre='Family Drama',
              plot='A child is kidnapped. A mother searches for the child and reunites her family.'),
        movie('M22','Thalapathy',['Rajinikanth'],['Surya']),
        movie('M23','Aster Voyage',['Nila Menon'],['Nova'],director='Mira Dev'),
        movie('M24','Aster Valley',['Rohan Das'],['Nila Menon'],director='Mira Dev'),
        movie('M25','Nila',['Aditi Rao'],['Mira Dev']),
        movie('M26','Starfall',['Nila Menon'],['Astronaut'],director='Kiran Bose'),
        movie('M27','High Tide',['Eva West'],languages=('English',)),
        movie('M28','The Quiet Lake',['Tom North'],languages=('English',)),
    ]


def catalog(size=None):
    raw=records()
    for i in range(len(raw),size or len(raw)):
        raw.append(dict(show_id=f'B{i:05}',title=f'Catalog Entry {i:05}',content_type='movie',
                        primary_cast=[dict(actor_name=f'Performer {i:05}',character_name=None)],
                        supporting_cast=[],director_name=None,genre='Drama',themes=[],
                        audio_languages=['Tamil'],plot_summary='A village family faces an unexpected problem.',
                        released_date='2020-01-01'))
    rows=[normalize_movie(r,'synthetic-relevance') for r in raw]
    aliases,_=build_aliases(rows)
    # Custom aliases intentionally collide with another movie's character/title.
    for alias,target in [('Nova','Nila Menon'),('The Navigator','Dev Patel')]:
        aliases.append(dict(actor_name=target,actor_norm=normalize(target),alias=alias,
                            alias_norm=normalize(alias),requires_context=False,
                            provenance='synthetic_fixture',note=''))
    lexicon=Counter()
    for row in rows:
        fields=[row['title'],row['plot_summary'],*row['cast_names'],*row['character_names'],
                *row['directors'],*row['genres'],*row['themes']]
        lexicon.update(set(normalize(' '.join(fields)).split()))
    cat=Catalog([asset_from_row(row) for row in rows],lexicon=dict(lexicon),doc_total=len(rows),
                generation=dict(generation_id=1,doc_count=len(rows),asset_count=len(rows)),
                actor_aliases=aliases)
    cat.metadata={row['show_id']:row for row in rows}
    return cat


def _case(query, expected=None, *, group, mode=None, kinds=None, forbidden=(), clarify=False):
    return dict(query=query, expected=None if expected is None else sorted(expected),group=group,
                mode=mode,kinds=kinds,forbidden=list(forbidden),clarify=clarify)


CASES=[
    _case(q,ids,group='documented',kinds=kinds) for q,ids,kinds in [
        ('Parasakthi',{'M01'},['title']),('Parimila',{'M02'},['title']),
        ('Parimala and Co',{'M02'},['title']),('Vijay movies',{'M03','M04'},['actor']),
        ('Thalapathy movies',{'M03','M04'},['actor']),('Talabadi movies',{'M03','M04'},['actor']),
        ('Ilayathalapathy movies',{'M03','M04'},['actor']),
        ('Parasakthi Uncensored Version',{'M01'},['title']),
        ('high',{'M27'},['title']),('2',{'M09'},['title']),('Ka',{'M10'},['title']),
        ('Movies with a character named Vijay',{'M05'},['character']),
        ('Movies directed by Shankar',{'M03'},['director'])]]
CASES += [_case(q,ids,group='roles',kinds=[kind]) for q,ids,kind in [
    ('movies starring Vijay',{'M03','M04'},'actor'),
    ('actor Vijay films',{'M03','M04'},'actor'),
    ('character Vijay movies',{'M05'},'character'),
    ('movies with character Thalapathy',{'M06'},'character'),
    ('play Thalapathy',{'M22'},'title'),
    ('Thalapathy',{'M22'},'title'),
    ('Salman Khan movies',{'M11'},'actor'),
    ('movies with a character named Salman Khan',{'M12'},'character'),
    ('Sallu Bhai films',{'M11'},'actor'),
    ('character Sallu Bhai',{'M15'},'character'),
    ('Suriya films',{'M12'},'actor'),
    ('Dev Patel movies',{'M16'},'actor'),
    ('movies starring Dev Patel',{'M16'},'actor'),
    ('movies with a character named Dev Patel',{'M17'},'character'),
    ('movies directed by Leela Rao',{'M14','M16','M19'},'director'),
    ('movies starring Leela Rao',{'M18'},'actor'),
    ('movies with character Shankar',{'M13'},'character'),
    ('movies starring Shankar',{'M14'},'actor'),
    ('Nila Menon movies',{'M23','M26'},'actor'),
    ('movies with a character named Nila Menon',{'M24'},'character'),
    ('Nova movies',{'M23','M26'},'actor'),
    ('character Nova',{'M23'},'character'),
    ('The Navigator movies',{'M16'},'actor'),
    ('movies directed by Mira Dev',{'M23','M24'},'director'),
    ('movies with character Mira Dev',{'M25'},'character'),
]]
CASES += [_case(q,ids,group='titles',kinds=['title']) for q,ids in [
    ('Parimala',{'M02'}),('play parasakthi',{'M01'}),('Ninna Preethiya',{'M07'}),
    ('Copper Horizn',{'M16'}),('Copper Horizon',{'M16'}),('Quiet Lake',{'M28'}),
    ('"The Gift"',{'M20'}),('Nila',{'M25'}),('Aster Voyage',{'M23'}),
    ('play High',{'M27'}),('play Family',{'M21'}),('Starfall',{'M26'}),
]]
CASES += [_case(q,group='safety',forbidden=forbidden,clarify=clarify) for q,forbidden,clarify in [
    ('a man finds the gift in a village',['title'],False),
    ('a child is kidnapped',['title','actor','character','director'],False),
    ('a woman named Parimala rescues a child',['title'],False),
    ('a superstar saves a village',['actor'],False),
    ('a highway robbery',['title'],False),
    ('a high speed chase',['title'],False),
    ('family films',['title'],False),
    ('feel-good movies',['title'],False),
    ('movies starring Unknown Person',[],True),
    ('movies directed by Unknown Person',[],True),
    ('scene where a child escapes',[],True),
    ('original language Tamil movies',[],True),
    ('safe for kids',[],True),
]]
