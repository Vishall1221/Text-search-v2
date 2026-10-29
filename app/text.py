"""Text normalisation shared by the importer and the query planner.

Design rules (from the V2 plan):
* Preserve the original query; normalise a copy.
* Unicode normalisation + case folding + whitespace cleanup + controlled punctuation.
* Never collapse repeated letters in names (the reference code's "mmovie -> movie"
  rule is deliberately NOT used on entity text).
* Accent folding is applied to Latin letters only, so Devanagari/Tamil/... marks survive.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_QUOTE_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "ʼ": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "–": "-", "—": "-", "−": "-",
})

_PII_EMAIL = re.compile(r"[a-zA-Z0-9._+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_PII_ID = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")
_PII_PHONE = re.compile(r"(?:(?:\+91|91|0)\s*-?\s*[6-9]\d{4}\s*-?\s*\d{5}\b|(?<!\d)[6-9]\d{9}\b)")


def _is_latin(ch: str) -> bool:
    o = ord(ch)
    return o < 0x250 or 0x1E00 <= o <= 0x1EFF


def strip_accents(text: str) -> str:
    """Remove combining marks that belong to Latin letters only."""
    out: list[str] = []
    for ch in text:
        if _is_latin(ch):
            decomposed = unicodedata.normalize("NFKD", ch)
            out.append("".join(c for c in decomposed if not unicodedata.combining(c)))
        else:
            out.append(ch)
    return "".join(out)


def fold(text: str) -> str:
    """NFKC + quote/dash unification + Latin accent folding + case folding.

    Keeps punctuation and apostrophes (PostgreSQL's parser is given this form so the
    index side and the query side tokenise identically).
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).translate(_QUOTE_MAP)
    t = strip_accents(t).casefold()
    return re.sub(r"\s+", " ", t).strip()


def normalize(text: str) -> str:
    """Matching form: folded, possessives removed, punctuation -> spaces, digits ranges kept."""
    t = fold(text)
    t = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " to ", t)         # 2010-2015 -> 2010 to 2015
    t = re.sub(r"(\w)'s\b", r"\1", t)                      # Code Cipher's -> code cipher
    t = t.replace("'", "")                                 # don't -> dont ; o'brien -> obrien
    # punctuation -> space, but keep letters, digits and combining marks (Indic vowel signs are marks,
    # which a plain \w class would delete) plus the zero-width joiners used inside Indic words
    t = "".join(ch if (ch.isalnum() or ch.isspace() or ch in "‌‍"
                       or unicodedata.category(ch)[0] == "M") else " " for ch in t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int


def tokenize(normalized: str) -> list[Token]:
    return [Token(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", normalized)]


def words(text: str) -> list[str]:
    return normalize(text).split()


def scrub_pii(text: str) -> tuple[str, bool]:
    """Adapted from the reference service's safety scrubber (phone/email/ID redaction).

    Used so personal data is not written to logs or caches; the query is still searched
    in its scrubbed form.
    """
    clean, hit = text, False
    for pattern, tag in ((_PII_EMAIL, "[email]"), (_PII_ID, "[id]"), (_PII_PHONE, "[phone]")):
        if pattern.search(clean):
            clean, hit = pattern.sub(tag, clean), True
    return clean, hit


# ---------------------------------------------------------------------------
# Edit distance (optimal string alignment: a transposition counts as ONE edit)
# ---------------------------------------------------------------------------

def osa_distance(a: str, b: str, limit: int = 3) -> int:
    """Bounded OSA distance; returns ``limit + 1`` as soon as it must exceed ``limit``."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            val = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                val = min(val, prev2[j - 2] + 1)
            cur[j] = val
            row_min = min(row_min, val)
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return min(prev[lb], limit + 1)


def trigrams(s: str) -> set[str]:
    padded = f"  {s} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def singularize(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
