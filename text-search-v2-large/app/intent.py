"""Local grammar for explicit entity roles, independent of catalog names.

The caller supplies the start of a possible name. Only the adjacent cue region
is examined; unrelated earlier words cannot change that name's role.
"""
from __future__ import annotations

from app.vocab import ACTOR_CUES, DIRECTOR_CUES, TITLE_REQUEST_CUES


def role_context(tokens: list[str], start: int) -> tuple[str | None, set[int]]:
    """Return an explicit role and positions of its grammatical introduction.

    Examples: ``starring NAME``, ``directed by NAME``, ``character named NAME``,
    ``movie called NAME``. Bare ``with NAME`` is intentionally not an actor cue.
    No name values, fuzzy matching, or catalog interpretation live here.
    """
    if start <= 0 or start > len(tokens):
        return None, set()
    i = start - 1
    used: set[int] = set()

    def articles():
        nonlocal i
        while i >= 0 and tokens[i] in {'a', 'an', 'the'}:
            used.add(i)
            i -= 1

    articles()
    connector = tokens[i] if i >= 0 and tokens[i] in {'named', 'called', 'name', 'by'} else None
    if connector:
        used.add(i)
        i -= 1
        articles()
    if i < 0:
        return None, set()

    cue = tokens[i]
    role = None
    if cue in DIRECTOR_CUES:
        role = 'director'
    elif connector != 'by':
        if cue in {'character', 'characters', 'role', 'roles'}:
            role = 'character'
        elif cue in ACTOR_CUES - {'with'}:
            role = 'actor'
        elif cue in TITLE_REQUEST_CUES - {'play', 'watch'}:
            role = 'title'
        elif connector in {'named', 'called', 'name'} and cue in {'movie', 'film', 'title'}:
            role = 'title'
    if role is None:
        return None, set()
    used.add(i)
    i -= 1
    articles()
    return role, used
