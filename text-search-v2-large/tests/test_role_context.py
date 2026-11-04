"""Explicit role grammar must not leak across content or Boolean operators."""
import pytest

from app.intent import role_context


@pytest.mark.parametrize('prefix,role', [
    ('starring', 'actor'), ('featuring', 'actor'), ('starred', 'actor'),
    ('cast', 'actor'), ('actor', 'actor'), ('actress named', 'actor'),
    ('the actor called', 'actor'), ('director', 'director'),
    ('directors', 'director'), ('directed by', 'director'),
    ('directed by the', 'director'), ('character', 'character'),
    ('a character named', 'character'), ('the character called', 'character'),
    ('characters named', 'character'), ('role named', 'character'),
    ('character name', 'character'), ('title', 'title'), ('titled', 'title'),
    ('movie called', 'title'), ('film named', 'title'),
])
def test_adjacent_role_grammar(prefix, role):
    tokens = prefix.split() + ['somebody']
    assert role_context(tokens, len(tokens) - 1) == (role, set(range(len(tokens) - 1)))


@pytest.mark.parametrize('prefix', [
    '', 'with', 'watch', 'play', 'movie', 'actor and', 'actor not',
    'actor without', 'actor rescues', 'character named and', 'director and by',
    'actor by', 'named', 'called', 'movie about', 'character named not',
])
def test_role_does_not_cross_unrelated_words(prefix):
    tokens = prefix.split() + ['somebody']
    assert role_context(tokens, len(tokens) - 1) == (None, set())


def test_only_cue_region_is_consumed():
    tokens = 'movies without a character named somebody'.split()
    assert role_context(tokens, 5) == ('character', {2, 3, 4})


def test_invalid_name_positions_are_safe():
    assert role_context(['actor', 'somebody'], -1) == (None, set())
    assert role_context(['actor', 'somebody'], 3) == (None, set())
