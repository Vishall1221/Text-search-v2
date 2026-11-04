"""Pure JSON validation/normalization. No database, model or network access."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from app.text import normalize
from app.vocab import LANGUAGE_ALIASES

DEFAULT_ALIASES = Path(__file__).with_name('actor_aliases.json')


def text(value, label, *, required=False):
    if value is None and not required:
        return ''
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f'{label}: expected {"nonempty " if required else ""}text')
    return value.strip()


def strings(value, label):
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r'[,;|/]', value)
    if not isinstance(value, list):
        raise ValueError(f'{label}: expected a string or list of strings')
    out = {}
    for v in value:
        v = text(v, label)
        if v:
            out.setdefault(normalize(v), v)
    return list(out.values())


def language(value):
    value = text(value, 'language', required=True)
    norm = normalize(value)
    return LANGUAGE_ALIASES.get(norm, value.upper())


def cast(value, label):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{label}: expected a list')
    out = []
    for member in value:
        if not isinstance(member, dict):
            raise ValueError(f'{label}: each cast member must be an object')
        actor = text(member.get('actor_name'), label+'.actor_name', required=True)
        char = member.get('character_name')
        char = text(char, label+'.character_name') or None
        out.append({'actor_name': actor, 'character_name': char})
    return out


def parse_date(value, label):
    if value in (None, ''):
        return None
    try:
        value = text(value, label)
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Invalid date format')
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label}: expected YYYY-MM-DD or null') from exc


def normalize_movie(raw, filename, released_date_kind='platform'):
    if not isinstance(raw, dict):
        raise ValueError('each movie must be an object')
    if released_date_kind not in {'unknown', 'original', 'platform'}:
        raise ValueError('released_date_kind must be unknown, original or platform')
    primary = cast(raw.get('primary_cast'), 'primary_cast')
    supporting = cast(raw.get('supporting_cast'), 'supporting_cast')
    names = {normalize(c['actor_name']): c['actor_name'] for c in primary+supporting}
    chars = {normalize(c['character_name']): c['character_name'] for c in primary+supporting if c['character_name']}
    genres = strings(raw.get('genres', raw.get('genre')), 'genre')
    directors = strings(raw.get('directors', raw.get('director_name')), 'director_name')
    source_date = parse_date(raw.get('released_date'), 'released_date')
    orig = parse_date(raw.get('original_release_date'), 'original_release_date')
    platform = parse_date(raw.get('platform_release_date'), 'platform_release_date')
    if source_date and released_date_kind != 'unknown':
        explicit = orig if released_date_kind == 'original' else platform
        if explicit and explicit != source_date:
            raise ValueError('released_date conflicts with the explicitly declared date')
        if released_date_kind == 'original':
            orig = source_date
        else:
            platform = source_date
    blob = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    result = {
        'show_id': text(raw.get('show_id'), 'show_id', required=True),
        'title': text(raw.get('title'), 'title', required=True),
        'content_type': text(raw.get('content_type'), 'content_type', required=True).casefold(),
        'plot_summary': text(raw.get('plot_summary'), 'plot_summary'),
        'themes': strings(raw.get('themes'), 'themes'),
        'genres': genres, 'genres_norm': [normalize(g) for g in genres],
        'primary_language': language(raw.get('primary_language')),
        'audio_languages': list(dict.fromkeys(language(v) for v in strings(
            raw.get('audio_languages', raw.get('AudioLanguages')), 'audio_languages'))),
        'primary_cast': primary, 'supporting_cast': supporting,
        'cast_names': list(names.values()), 'cast_norm': list(names),
        'character_names': list(chars.values()), 'character_norm': list(chars),
        'directors': directors, 'directors_norm': [normalize(d) for d in directors],
        'source_released_date': source_date,
        'release_date': (source_date or platform) if released_date_kind == 'platform' else (
            (source_date or orig) if released_date_kind == 'original' else None),
        'original_release_date': orig,
        'platform_release_date': platform, 'source_metadata': raw,
        'source_file': filename, 'source_sha256': hashlib.sha256(blob.encode()).hexdigest(),
    }
    result['title_norm'] = normalize(result['title'])
    if not result['title_norm'] or any(not n for n in result['cast_norm']):
        raise ValueError('title and cast names must contain letters or numbers')
    return result


def read_movies(path, released_date_kind='platform'):
    path = Path(path)
    files = sorted(path.glob('*.json')) if path.is_dir() else [path]
    if not files:
        raise ValueError('No JSON files found')
    movies, errors, seen = [], [], set()
    for file in files:
        try:
            payload = json.loads(file.read_text(encoding='utf-8-sig'))
            rows = payload.get('movies', payload) if isinstance(payload, dict) else payload
            rows = rows if isinstance(rows, list) else [rows]
            for i, raw in enumerate(rows, 1):
                try:
                    movie = normalize_movie(raw, file.name, released_date_kind)
                    if movie['show_id'] in seen:
                        raise ValueError(f'duplicate show_id {movie["show_id"]!r}; put dubbed tracks in audio_languages')
                    seen.add(movie['show_id'])
                    movies.append(movie)
                except ValueError as exc:
                    errors.append(f'{file.name} record {i}: {exc}')
        except (OSError, ValueError) as exc:
            errors.append(f'{file.name}: {exc}')
    if not movies and not errors:
        errors.append('Empty catalog; refusing to replace an active catalog with no movies')
    if errors:
        raise ValueError('Validation failed before database writes:\n'+'\n'.join(errors))
    return movies


def build_aliases(movies, path=DEFAULT_ALIASES):
    payload = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    groups = payload.get('actors')
    if not isinstance(groups, list):
        raise ValueError('aliases JSON must contain an actors list')
    if any(k in payload for k in ('movies', 'titles', 'characters')):
        raise ValueError('Only actor/actress aliases are supported in this schema')
    actors = {n: name for m in movies for n, name in zip(m['cast_norm'], m['cast_names'])}
    rows, skipped = {}, []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError('Each alias group must be an object')
        if not isinstance(group.get('requires_context', False), bool):
            raise ValueError('requires_context must be a JSON boolean')
        catalog_names = strings(group.get('catalog_names'), 'catalog_names')
        aliases = strings(group.get('aliases'), 'aliases')
        if not catalog_names or not aliases:
            raise ValueError('Alias groups need nonempty catalog_names and aliases')
        targets = [normalize(n) for n in catalog_names if normalize(n) in actors]
        if not targets:
            skipped.append(catalog_names[0])
        for target in targets:
            for alias in aliases:
                an = normalize(alias)
                if not an or an == target:
                    continue
                row = {'actor_name': actors[target], 'actor_norm': target, 'alias': alias, 'alias_norm': an,
                       'requires_context': bool(group.get('requires_context', False)),
                       'provenance': text(group.get('provenance', 'custom'), 'provenance', required=True),
                       'note': text(group.get('note'), 'note')}
                old = rows.get((target, an))
                if old and old != row:
                    raise ValueError(f'Conflicting alias definition for {alias!r}')
                rows[target, an] = row
    return list(rows.values()), skipped
