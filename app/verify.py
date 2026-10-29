"""Literal verification of quoted / "says ..." dialogue requests.

Full-text search finds *candidates*; this decides whether a candidate really contains the words, in
order, as whole words (normalised: case, accents, punctuation). By default the phrase may also span
two adjacent turns (``DIALOGUE_ACROSS_TURNS``); within-turn matches are always preferred.
"""

from __future__ import annotations

from typing import Any

from app.text import normalize


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def verify_phrase(turns: list[dict[str, Any]], phrase: str, across_turns: bool = True) -> str | None:
    """-> 'turn' | 'across_turns' | None."""
    needle = normalize(phrase)
    if not needle or not turns:
        return None
    originals = [normalize(t.get("original") or "") for t in turns]
    glosses = [normalize(t.get("gloss") or "") for t in turns]
    for o, g in zip(originals, glosses):
        if _contains(o, needle) or _contains(g, needle):
            return "turn"
    if across_turns and len(turns) > 1:
        for texts in (originals, glosses):
            for a, b in zip(texts, texts[1:]):
                if _contains(f"{a} {b}", needle) and not (_contains(a, needle) or _contains(b, needle)):
                    return "across_turns"
    return None
