from datetime import date
import pytest
from app.planner import plan_query, build_channels
from app.gazetteer import Entity

def plan(q,cat):
    return plan_query(q,cat,date(2026,11,4))

@pytest.mark.parametrize('query,key',[
    ('Sunrise Promise','F1'),('thalapathy movies','F1'),('thalapthy films','F1'),
    ('Vaigai Puyal movies','F1'),('sallu bhai films','F2'),('nadipu nayagan movies','F3'),('SRK movies','F6')])
def test_titles_and_actor_aliases(query,key,catalog):
    p=plan(query,catalog)
    assert p.clarification is None and p.allowed=={key}
    assert p.mode in {'title','browse'}

def test_multiaudio_and_or_and_negation(catalog):
    assert plan('Tamil and Hindi movies',catalog).allowed=={'F1','F3','F6'}
    assert plan('English or Telugu movies',catalog).allowed=={'F2','F4','F5'}
    assert plan('movies available in Hindi',catalog).allowed=={'F1','F2','F3','F4','F6'}
    assert plan('Tamil dubbed movies',catalog).allowed=={'F1','F3','F6'}
    assert plan('Hindi movies except Suriya',catalog).allowed=={'F1','F2','F4','F6'}

@pytest.mark.parametrize('query', ['feel-good films','heartwarming movies','uplifting films','comfort watch'])
def test_soft_mood_preferences_have_search_text_and_no_false_genre(query,catalog):
    p=plan(query,catalog)
    assert p.mode=='catalog' and p.dense_text and p.terms and p.approximate
    assert not any(f.kind=='genre' for f in p.facets)
    assert any('preference' in note for note in p.notes)

@pytest.mark.parametrize('q,key',[('family films','F1'),('funny movies','NONE'),('love story films','F6'),('spooky movies','F4')])
def test_genre_synonyms_and_absent_category(q,key,catalog):
    p=plan(q,catalog)
    assert p.allowed == (set() if key=='NONE' else {key})

def test_dynamic_theme_entity(catalog):
    assert plan('betrayal movies',catalog).allowed=={'F2'}
    assert plan('3 abduction movies',catalog).limit==3

@pytest.mark.parametrize('q',['scene where he escapes','he says I will return','find me the scenes with Vijay','exact dialogue'])
def test_scene_and_dialogue_requests_are_explicitly_unsupported(q,catalog):
    p=plan(q,catalog)
    assert p.clarification and p.clarification.reason=='unsupported_scene'

@pytest.mark.parametrize('q',['family friendly movies','safe for kids','original language Tamil movies'])
def test_missing_suitability_and_original_language_do_not_become_filters(q,catalog):
    assert plan(q,catalog).mode=='clarify'

def test_unknown_actor_never_becomes_unrestricted_retrieval(catalog):
    assert plan('movies starring Brad Pitt',catalog).mode=='clarify'

def test_ordinary_spelling_only_changes_lexical_terms(catalog):
    p=plan('a child is kidnpped',catalog)
    assert 'kidnapped' in p.terms
    assert 'kidnpped' in p.dense_text
    assert any(c.method=='lexicon' for c in p.corrections)

def test_narrative_titles_person_words_and_negation(catalog):
    for q in ['a man finds the gift in a village','the actress cries','he has no money and no hope']:
        p=plan(q,catalog)
        assert p.mode!='clarify' and not any(f.kind=='title' for f in p.facets)

def test_dates_use_platform_release_and_preserve_sort(catalog):
    p=plan('latest Tamil movies after 2021',catalog)
    assert p.sort=='latest' and p.allowed=={'F1','F3'}
    assert any('ZEE5' in n for n in p.notes)

def test_two_catalog_channels_only(catalog):
    p=plan('a detective rescues a child',catalog)
    assert {c.name for c in build_channels(p)}=={'catalog_lex','catalog_dense'}

def test_alias_collision_requests_clarification(catalog):
    catalog._add('shared nickname',Entity('actor','Vijay','vijay',via_alias=True),True)
    catalog._add('shared nickname',Entity('actor','Suriya','suriya',via_alias=True),True)
    assert plan('shared nickname movies',catalog).clarification.reason=='ambiguous_name'

def test_generic_alias_does_not_hijack_narrative(catalog):
    from app.gazetteer import Span
    catalog.guarded_aliases['superstar']=[Entity('actor','Vijay','vijay',via_alias=True)]
    assert not any(f.kind=='actor' for f in plan('a superstar saves a village',catalog).facets)
    assert plan('superstar movies',catalog).allowed=={'F1'}

def test_quoted_titles_and_titles_containing_scene_are_still_titles(catalog):
    from dataclasses import replace
    from app.gazetteer import Catalog
    assert plan('"The Gift"',catalog).allowed=={'F5'}
    special=replace(catalog.assets['F1'],title='The Scene',title_norm='the scene')
    cat=Catalog([special],doc_total=10)
    assert plan('play The Scene',cat).mode=='title'

def test_fuzzy_partial_names_and_aliases_remain_available(catalog):
    assert plan('thalapathyy movies',catalog).allowed=={'F1'}
    assert plan('shahrukh khan films',catalog).allowed=={'F6'}

def test_negated_soft_preference_does_not_turn_positive(catalog):
    assert plan('movies not very uplifting',catalog).clarification.reason=='unsupported_preference_exclusion'

def test_absent_genres_in_plot_narratives_stay_words(catalog):
    p=plan('a movie about an adventure in a jungle',catalog)
    assert p.empty_reason is None and 'adventure' in p.terms

def test_fiction_kept_separate_from_science_fiction_after_registry_move(catalog):
    from dataclasses import replace
    from app.gazetteer import Catalog
    period=replace(catalog.assets['F5'],genres=['Period Fiction'],genres_norm=['period fiction'])
    cat=Catalog([period],doc_total=40)
    assert plan('fiction movies',cat).allowed=={'F5'}
    assert plan('sci fi movies',cat).allowed==set()
