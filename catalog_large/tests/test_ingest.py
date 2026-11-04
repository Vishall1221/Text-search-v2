"""Catalog-only import contracts; no change to the existing search algorithm."""
import copy
import json
from datetime import date

import pytest

from catalog_large.ingest import ingest, passages
from catalog_large.source import build_aliases, normalize_movie, read_movies


def movie(show_id='M1', actor='Vijay'):
    return {'show_id': show_id, 'title': 'Example '+show_id, 'content_type': 'movie',
            'plot_summary': 'A child is kidnapped. A detective rescues the child.',
            'themes': ['Rescue', 'Family'], 'genre': 'Action', 'primary_language': 'Tamil',
            'AudioLanguages': ['Tamil', 'Hindi', 'tamil'], 'released_date': '2021-11-28',
            'primary_cast': [{'actor_name': actor, 'character_name': 'Arun'}],
            'supporting_cast': [{'actor_name': 'Vadivelu', 'character_name': None}],
            'director_name': None}


def write(tmp_path, rows):
    path = tmp_path / 'movies.json'
    path.write_text(json.dumps(rows), encoding='utf-8')
    return path


def test_source_mapping_preserves_language_cast_and_date_meaning():
    raw = movie()
    m = normalize_movie(raw, 'movies.json')
    assert m['primary_language'] == 'TAMIL'
    assert m['audio_languages'] == ['TAMIL', 'HINDI']
    assert m['primary_cast'][0]['character_name'] == 'Arun'
    assert m['supporting_cast'][0]['character_name'] is None
    assert m['cast_names'] == ['Vijay', 'Vadivelu']
    assert m['original_release_date'] is None
    assert m['release_date'] == m['platform_release_date'] == date(2021, 11, 28)
    assert m['source_metadata'] == raw
    assert m['directors'] == [] and m['genres'] == ['Action']


def test_audio_string_and_no_inference_from_audio():
    raw = movie()
    raw['AudioLanguages'] = 'Tamil, Hindi | Telugu; Tamil'
    assert normalize_movie(raw, 'x')['audio_languages'] == ['TAMIL','HINDI','TELUGU']
    del raw['primary_language']
    m = normalize_movie(raw, 'x')
    assert m['primary_language'] is None
    assert m['audio_languages'] == ['TAMIL','HINDI','TELUGU']


def test_missing_optional_metadata_is_not_invented():
    raw = movie()
    for k in ('AudioLanguages', 'released_date', 'primary_cast', 'supporting_cast', 'plot_summary'):
        raw.pop(k)
    m = normalize_movie(raw, 'x')
    assert m['audio_languages'] == [] and m['release_date'] is None
    assert m['cast_names'] == [] and m['plot_summary'] == ''


def test_declared_original_or_unknown_dates():
    assert normalize_movie(movie(), 'x', 'original')['original_release_date'] == date(2021,11,28)
    assert normalize_movie(movie(), 'x', 'unknown')['release_date'] is None
    raw = movie()
    raw['platform_release_date'] = '2022-01-01'
    with pytest.raises(ValueError, match='conflicts'):
        normalize_movie(raw, 'x')


@pytest.mark.parametrize('edit', [
    {'show_id': ''}, {'title': None}, {'released_date': '2020-02-31'},
    {'primary_cast': [{'actor_name': 'Vijay', 'character_name': 42}]},
    {'themes': [42]}, {'primary_cast': 'Vijay'},
])
def test_invalid_records_fail_before_database_access(tmp_path, edit):
    raw = {**movie(), **edit}
    with pytest.raises(ValueError):
        ingest(write(tmp_path, [raw]), url='not a database URL', lexical_only=True)


@pytest.mark.parametrize('container', ['array','wrapper','single'])
def test_json_containers_and_utf8_bom(tmp_path, container):
    payload = [movie()] if container == 'array' else ({'movies': [movie()]} if container == 'wrapper' else movie())
    path = write(tmp_path, payload)
    path.write_text(path.read_text(), encoding='utf-8-sig')
    assert len(read_movies(path)) == 1


def test_full_validation_collects_errors_and_rejects_duplicate_ids(tmp_path):
    rows = [movie(), movie(), {**movie('M2'), 'title': None}]
    with pytest.raises(ValueError) as exc:
        read_movies(write(tmp_path, rows))
    assert 'duplicate show_id' in str(exc.value) and 'record 3' in str(exc.value)
    with pytest.raises(ValueError, match='Empty catalog'):
        read_movies(write(tmp_path, []))


def test_aliases_only_target_exact_actor_names():
    movies = [normalize_movie(movie(), 'x'), normalize_movie(movie('M2', 'Vijay Sethupathi'), 'x')]
    aliases, skipped = build_aliases(movies)
    thalapathy = [a for a in aliases if a['alias_norm'] == 'thalapathy']
    assert len(thalapathy) == 1 and thalapathy[0]['actor_norm'] == 'vijay'
    assert any(a['alias_norm'] == 'vaigai puyal' and a['actor_norm'] == 'vadivelu' for a in aliases)
    assert all(a['actor_norm'] in {'vijay','vadivelu'} for a in aliases)
    assert 'Salman Khan' in skipped
    role = [a for a in aliases if a['alias_norm'] == 'cm of tamilnadu'][0]
    assert role['requires_context'] and role['provenance'] == 'user_custom_role_label'


def test_requested_suriya_salman_and_actress_aliases():
    movies = [normalize_movie(movie(str(i), name), 'x') for i, name in enumerate(
        ['Suriya','Salman Khan','Nayanthara'])]
    aliases, _ = build_aliases(movies)
    mapping = {a['alias_norm']: a['actor_name'] for a in aliases}
    assert mapping['nadipu nayagan'] == 'Suriya'
    assert mapping['sallu bhai'] == mapping['tiger of bollywood'] == 'Salman Khan'
    assert mapping['lady superstar'] == 'Nayanthara'


def test_alias_collisions_preserve_all_candidates_not_arbitrary_winner(tmp_path):
    m = [normalize_movie(movie('1','A'), 'x'), normalize_movie(movie('2','B'), 'x')]
    path = tmp_path/'aliases.json'
    path.write_text(json.dumps({'actors': [
        {'catalog_names':['A'], 'aliases':['Star']}, {'catalog_names':['B'], 'aliases':['Star']}]}))
    rows, _ = build_aliases(m, path)
    assert {r['actor_name'] for r in rows if r['alias_norm']=='star'} == {'A','B'}
    path.write_text(json.dumps({'actors': [], 'movies': {'Example': ['Other title']}}))
    with pytest.raises(ValueError, match='Only actor'):
        build_aliases(m, path)


def test_catalog_only_passages_include_themes_and_fit_budget():
    raw = movie()
    raw['plot_summary'] = 'An explorer discovers a treasure. '*200
    m = normalize_movie(raw, 'x')
    docs, _ = passages([m], tokens=32)
    assert len(docs)>1 and all(d['token_count']<=32 for d in docs)
    combined = ' '.join(d['text'] for d in docs)
    assert 'Family' in combined and combined.count('treasure') == 200
    assert all('scene_id' not in d for d in docs)


def test_2000_movie_dry_run_needs_no_model_or_database(tmp_path):
    rows = [movie(f'M{i}') for i in range(2000)]
    for row in rows:
        row.pop('primary_language')
    path = write(tmp_path, rows)
    result = ingest(path, dry_run=True, url='invalid://must-not-connect')
    assert result['assets'] == 2000 and result['dry_run']
    assert result['aliases'] > 0 and result['released_date_kind'] == 'platform'
    assert result['missing_primary_language'] == 2000


@pytest.mark.parametrize('primary', [None, '', '   '])
def test_unknown_primary_language_is_reported_not_inferred(tmp_path, primary):
    raw = movie()
    raw['primary_language'] = primary
    raw['audio_languages'] = raw.pop('AudioLanguages')
    report = ingest(write(tmp_path, [raw]), dry_run=True)
    assert report['missing_primary_language'] == 1
    assert report['primary_languages'] == []
    assert report['missing_audio_languages'] == 0
