"""Date / year constraint extraction from normalised query tokens.

Only the *original release* year is ever constrained. If the catalog does not carry it, the
service reports that the constraint cannot be applied (it never substitutes platform-arrival
or ingestion dates, which would answer a different question).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.vocab import DECADE_WORDS

YEAR = re.compile(r"^(19\d{2}|20\d{2})$")
DECADE_NUM = re.compile(r"^(?:(19|20)?(\d)0)s$")      # 90s, 1990s, 2000s


@dataclass(frozen=True)
class YearRange:
    gte: int | None
    lte: int | None
    text: str
    excluded: tuple[tuple[int | None, int | None], ...] = ()
    included_years: tuple[int, ...] = ()

    def contains(self, year: int) -> bool:
        return ((self.gte is None or year >= self.gte) and (self.lte is None or year <= self.lte)
                and (not self.included_years or year in self.included_years)
                and not any((lo is None or year >= lo) and (hi is None or year <= hi)
                            for lo, hi in self.excluded))

    def describe(self) -> str:
        if self.included_years:
            return " or ".join(map(str, self.included_years))
        if self.excluded:
            included = YearRange(self.gte, self.lte, self.text).describe() if self.gte is not None or self.lte is not None else "all years"
            return included + "; excluding " + ", ".join(YearRange(a, b, "").describe() for a, b in self.excluded)
        if self.gte is not None and self.lte is not None:
            return str(self.gte) if self.gte == self.lte else f"{self.gte}-{self.lte}"
        if self.gte is not None:
            return f"{self.gte} or later"
        return f"{self.lte} or earlier"


def _decade_start(tok: str) -> int | None:
    if tok in DECADE_WORDS:
        return DECADE_WORDS[tok]
    m = DECADE_NUM.match(tok)
    if not m:
        return None
    century, d = m.group(1), int(m.group(2))
    if century:
        return int(century) * 100 + d * 10
    return (2000 if d in (0, 1) else 1900) + d * 10


def extract_years(tokens: list[str], consumed: set[int], today: date) -> tuple[YearRange | None, set[int]]:
    """Intersect every date bound, preserving explicitly negated year ranges as exclusions."""
    used: set[int] = set()
    found: list[YearRange] = []
    excluded: list[tuple[int | None, int | None]] = []
    labels: list[str] = []
    while True:
        constraint, indexes = _extract_one(tokens, consumed | used, today)
        if constraint is None:
            break
        first = min(indexes)
        preceding = first - 1
        while preceding >= 0 and tokens[preceding] in {"in", "the", "during", "released", "year", "years"} and preceding not in consumed | used:
            indexes.add(preceding)
            preceding -= 1
        if preceding >= 0 and preceding not in consumed | used and tokens[preceding] in {"not", "no", "except", "excluding", "without"}:
            excluded.append((constraint.gte, constraint.lte))
            indexes.add(preceding)
        else:
            found.append(constraint)
        labels.append(" ".join(tokens[i] for i in sorted(indexes)))
        used |= indexes
    if not used:
        return None, set()
    lower = [r.gte for r in found if r.gte is not None]
    upper = [r.lte for r in found if r.lte is not None]
    if len(found) > 1 and all(r.gte == r.lte and r.gte is not None for r in found):
        # "in 2015 and 2019" names discrete years; only "between ... and ..."
        # expands the years into a continuous range.
        discrete = tuple(sorted({r.gte for r in found}))
        return YearRange(min(discrete), max(discrete), " and ".join(labels), tuple(excluded), discrete), used
    return YearRange(max(lower) if lower else None, min(upper) if upper else None,
                     " and ".join(labels), tuple(excluded)), used


def _extract_one(tokens: list[str], consumed: set[int], today: date) -> tuple[YearRange | None, set[int]]:
    """Return (constraint, token indexes used). Looks only at tokens not already consumed."""
    used: set[int] = set()
    n = len(tokens)
    free = lambda i: 0 <= i < n and i not in consumed and i not in used            # noqa: E731

    def tok(i: int) -> str:
        return tokens[i] if 0 <= i < n else ""

    # --- ranges: "between 2010 and 2015", "from 2010 to 2015", "2010 to 2015"
    for i in range(n):
        if free(i) and YEAR.match(tok(i)):
            j = i + 1
            if (tok(j) in ("to", "till", "until") or (tok(j) == "and" and tok(i - 1) == "between")) and free(j + 1) and YEAR.match(tok(j + 1)):
                a, b = sorted((int(tok(i)), int(tok(j + 1))))
                used.update({i, j, j + 1})
                k = i - 1
                if free(k) and tok(k) in ("between", "from"):
                    used.add(k)
                return YearRange(a, b, f"{a}-{b}"), used

    # --- decades (optionally "early"/"mid"/"late")
    for i in range(n):
        if not free(i):
            continue
        start = _decade_start(tok(i))
        if start is not None:
            used.add(i)
            lo, hi = start, start + 9
            if free(i - 1) and tok(i - 1) in ("early", "mid", "late"):
                used.add(i - 1)
                lo, hi = {"early": (start, start + 3), "mid": (start + 3, start + 6), "late": (start + 6, start + 9)}[tok(i - 1)]
            for k in (i - 1, i - 2):
                if free(k) and tok(k) in ("in", "during", "from", "of", "the"):
                    used.add(k)
            return YearRange(lo, hi, f"{lo}-{hi}"), used

    # --- relative
    for i in range(n):
        if not free(i):
            continue
        t, nxt = tok(i), tok(i + 1)
        if t == "last" and nxt == "year" and free(i + 1):
            used.update({i, i + 1}); return YearRange(today.year - 1, today.year - 1, "last year"), used
        if t == "this" and nxt == "year" and free(i + 1):
            used.update({i, i + 1}); return YearRange(today.year, today.year, "this year"), used
        if t in ("last", "past") and tok(i + 2) in ("year", "years") and nxt.isdigit() and free(i + 1) and free(i + 2):
            k = int(nxt)
            if 1 <= k <= 50:
                used.update({i, i + 1, i + 2}); return YearRange(today.year - k, today.year, f"last {k} years"), used
        if t == "last" and nxt == "decade" and free(i + 1):
            used.update({i, i + 1}); return YearRange(today.year - 10, today.year, "last decade"), used

    # --- single year with a cue
    for i in range(n):
        if not (free(i) and YEAR.match(tok(i))):
            continue
        y = int(tok(i))
        prev, prev2 = tok(i - 1), f"{tok(i - 2)} {tok(i - 1)}".strip()
        for cue, rng in (
            ("earlier than", (None, y - 1)), ("older than", (None, y - 1)), ("prior to", (None, y - 1)),
            ("released after", (y + 1, None)), ("later than", (y + 1, None)), ("newer than", (y + 1, None)),
            ("released before", (None, y - 1)), ("released in", (y, y)), ("up to", (None, y)),
        ):
            if prev2 == cue and free(i - 1) and free(i - 2):
                used.update({i, i - 1, i - 2}); return YearRange(*rng, f"{cue} {y}"), used
        if free(i - 1):
            if prev in ("before", "pre"):
                used.update({i, i - 1}); return YearRange(None, y - 1, f"before {y}"), used
            if prev in ("until", "till"):
                used.update({i, i - 1}); return YearRange(None, y, f"until {y}"), used
            if prev in ("after", "post"):
                used.update({i, i - 1}); return YearRange(y + 1, None, f"after {y}"), used
            if prev in ("since", "from", "starting"):
                used.update({i, i - 1}); return YearRange(y, None, f"since {y}"), used
            if prev in ("in", "during", "of", "year"):
                used.update({i, i - 1}); return YearRange(y, y, str(y)), used
        if tok(i + 1) in ("onwards", "onward") and free(i + 1):
            used.update({i, i + 1}); return YearRange(y, None, f"{y} onwards"), used
        if tok(i + 1) == "or" and tok(i + 2) in ("later", "newer", "after") and free(i + 1) and free(i + 2):
            used.update({i, i + 1, i + 2}); return YearRange(y, None, f"{y} or later"), used
        if tok(i + 1) == "or" and tok(i + 2) in ("earlier", "older", "before") and free(i + 1) and free(i + 2):
            used.update({i, i + 1, i + 2}); return YearRange(None, y, f"{y} or earlier"), used
        # bare year
        used.add(i)
        return YearRange(y, y, str(y)), used

    return None, set()
