"""Query operators that are not catalog entities: sort, popularity, top-N limit, negation cues.

Rules were adapted from the reference service (top-N / rank extraction, popularity words
reported as unsupported) and tightened so descriptive text is not misread as an operator:
"new" / "classic" / "old" count as sort cues only next to a content noun ("new movies").
"""

from __future__ import annotations

from dataclasses import dataclass

from app.vocab import (CONTENT_TYPE_WORDS, LIMIT_CUES, LIMIT_NOUNS, NEGATION_CUES, NUMBER_WORDS, POPULARITY,
                       SORT_LATEST, SORT_OLDEST)

CONTENT_NOUNS = set(CONTENT_TYPE_WORDS) | {"releases", "release", "titles", "title", "ones", "stuff"}
ADDED_CUES = [("recently", "added"), ("newly", "added"), ("just", "added"), ("new", "on"), ("added", "recently")]
WEAK_SORT = {("new",), ("classic",), ("old", "classic")}         # only valid before a content noun


def match_at(tokens: list[str], i: int, seq: tuple[str, ...]) -> bool:
    return tuple(tokens[i:i + len(seq)]) == seq


def _sort_cue(tokens: list[str], i: int, consumed: set[int], seqs: list[tuple[str, ...]]) -> int:
    """Length of the cue starting at i (0 = none)."""
    for seq in sorted(seqs, key=len, reverse=True):
        if match_at(tokens, i, seq) and not any((i + k) in consumed for k in range(len(seq))):
            if seq in WEAK_SORT:
                nxt = tokens[i + len(seq)] if i + len(seq) < len(tokens) else ""
                if nxt not in CONTENT_NOUNS:
                    continue
            return len(seq)
    return 0


@dataclass
class Operators:
    sort: str | None = None                  # latest | oldest | added
    popularity: str | None = None            # the matched phrase, reported as unavailable
    limit: int | None = None
    used: set[int] = None                    # token indexes consumed


def extract_operators(tokens: list[str], consumed: set[int], entity_starts: set[int] | None = None) -> Operators:
    ops = Operators(used=set())
    n = len(tokens)
    i = 0
    while i < n:
        if i in consumed or i in ops.used:
            i += 1
            continue
        k = _sort_cue(tokens, i, consumed | ops.used, ADDED_CUES)
        if k and ops.sort is None:
            ops.sort = "added"; ops.used.update(range(i, i + k)); i += k; continue
        k = _sort_cue(tokens, i, consumed | ops.used, SORT_LATEST)
        if k and ops.sort is None:
            ops.sort = "latest"; ops.used.update(range(i, i + k)); i += k; continue
        k = _sort_cue(tokens, i, consumed | ops.used, SORT_OLDEST)
        if k and ops.sort is None:
            ops.sort = "oldest"; ops.used.update(range(i, i + k)); i += k; continue
        k = _sort_cue(tokens, i, consumed | ops.used, POPULARITY)
        if k and ops.popularity is None:
            ops.popularity = " ".join(tokens[i:i + k]); ops.used.update(range(i, i + k)); i += k; continue
        i += 1
    # --- top N ---------------------------------------------------------------------------
    for i in range(n):
        if i in consumed or i in ops.used:
            continue
        t = tokens[i]
        num = int(t) if t.isdigit() and len(t) <= 2 else NUMBER_WORDS.get(t)
        if not num or not 1 <= num <= 50:
            continue
        before = tokens[i - 1] if i > 0 and (i - 1) not in consumed and (i - 1) not in ops.used else ""
        after = tokens[i + 1] if i + 1 < n and (i + 1) not in consumed and (i + 1) not in ops.used else ""
        has_cue = before in LIMIT_CUES
        has_noun = after in LIMIT_NOUNS or after in CONTENT_NOUNS or after in {"scene", "scenes", "clip", "clips"}
        # "3 horror films", "10 tamil movies": the number directly precedes a recognised facet word
        has_noun = has_noun or (i + 1) in (entity_starts or set())
        if has_cue or has_noun:
            ops.limit = num
            ops.used.add(i)
            if has_cue:
                ops.used.add(i - 1)
            break
    # bare "top"/"first"/"best" with no number is just emphasis: leave them to noise filtering
    return ops


def negation_cue_at(tokens: list[str], i: int) -> int:
    """Length of a negation cue starting at i (0 = none). Longest cue wins."""
    best = 0
    for seq in NEGATION_CUES:
        if match_at(tokens, i, seq):
            best = max(best, len(seq))
    return best
