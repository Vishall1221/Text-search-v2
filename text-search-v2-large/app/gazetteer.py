"""In-memory catalog snapshot and entity index (the "gazetteer").

All catalog names (titles, aliases, actors, characters, genres, languages) are read from the
database; none are hardcoded. The snapshot is rebuilt when the active generation changes or
after ``catalog_cache_seconds``.

Entity resolution, in the order the planner uses it:
  1. exact longest-span match on the normalised phrase,
  2. indexed whole-word title fragments in a title/listing request,
  3. transliteration-skeleton match (``raktha vajram`` == ``rakta vajram``),
  4. fuzzy candidates: shared trigrams -> bounded OSA edit distance.
The planner (not this module) decides whether a fuzzy candidate may be auto-corrected.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from app.text import normalize, osa_distance, singularize, trigrams
from app.vocab import (CONTENT_TYPE_WORDS, GENRE_SYNONYMS, LANGUAGE_ALIASES, NOISE,
                       TITLE_REQUEST_CUES, EXPLICIT_PERSON_SLOTS, NARRATIVE_TITLE_BLOCKERS,
                       CATALOG_MARKERS, SORT_LATEST, SORT_OLDEST)

KIND_PRIORITY = {"title": 0, "actor": 1, "director": 2, "character": 3, "genre": 4, "theme": 5, "language": 6, "content_type": 7}
FUZZY_KINDS = ("title", "actor", "director", "character")


# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Entity:
    kind: str                 # title | actor | character | genre | language | content_type
    canonical: str            # display value
    norm: str                 # normalised canonical form (identity within a kind)
    partial: bool = False     # matched through one word of a longer name
    via_alias: bool = False

    @property
    def ident(self) -> tuple[str, str]:
        return (self.kind, self.norm)


@dataclass
class AssetInfo:
    asset_key: str
    asset_id: str
    audio_languages: list[str]
    themes: list[str]
    title: str
    title_norm: str
    description: str
    genres: list[str]
    genres_norm: list[str]
    cast_names: list[str]
    cast_norm: list[str]
    characters: list[str]
    characters_norm: list[str]
    directors: list[str]
    content_type: str | None
    release_year: int | None
    release_date: date | None
    platform_release_date: date | None
    thumbnail_image_url: str | None
    aliases: list[str] = field(default_factory=list)


@dataclass
class Span:
    start: int                         # token index, inclusive
    end: int                           # token index, exclusive
    phrase: str
    entities: list[Entity]
    match: str = "exact"               # exact | title_fragment | skeleton
    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class Candidate:
    entity: Entity
    distance: int
    phonetic: bool = False


# ---------------------------------------------------------------------------------------
_SKEL_H = re.compile(r"([tdkgbpj])h")
_SKEL_REP = re.compile(r"(.)\1+")


_NUMBER_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def skeleton(text: str) -> str:
    """Transliteration skeleton for Latin-script names (th->t, w->v, doubled letters collapsed ...).

    Number words become digits inside multi-word names ("three idiots" == "3 idiots"). A single word is
    left alone so a bare "three" in a description cannot be mistaken for a film called "3".
    """
    parts = text.split()
    if len(parts) > 1:
        parts = [_NUMBER_WORDS.get(w, w) for w in parts]
    out = []
    for word in parts:
        w = _SKEL_H.sub(r"\1", word)
        w = w.replace("ee", "i").replace("oo", "u").replace("w", "v").replace("z", "j").replace("q", "k")
        w = _SKEL_REP.sub(r"\1", w)
        if len(w) > 3 and w.endswith("y"):
            w = w[:-1] + "i"
        out.append(w)
    return " ".join(out)


def _strip_article(norm: str) -> str:
    return re.sub(r"^(?:the|a|an)\s+", "", norm)


# ---------------------------------------------------------------------------------------
class Catalog:
    """Immutable snapshot of one active generation."""

    def __init__(self, assets: Iterable[AssetInfo], *, lexicon: dict[str, int] | None = None,
                 generation: dict[str, Any] | None = None,
                 doc_total: int = 0, actor_aliases: list[dict] | None = None) -> None:
        self.assets: dict[str, AssetInfo] = {a.asset_key: a for a in assets}
        self.lexicon: dict[str, int] = lexicon or {}
        self.generation: dict[str, Any] = generation or {}
        self.doc_total = max(doc_total, 1)
        self.exact: dict[str, list[Entity]] = defaultdict(list)
        self.skel: dict[str, list[Entity]] = defaultdict(list)
        self.tri: dict[str, set[int]] = defaultdict(set)
        self.fuzzy_keys: list[tuple[str, Entity]] = []
        self.title_assets: dict[str, set[str]] = defaultdict(set)       # title/alias norm -> asset keys
        self.title_words: dict[str, set[str]] = defaultdict(set)
        self.title_entities: dict[str, Entity] = {}
        self.actor_assets: dict[str, set[str]] = defaultdict(set)
        self.character_assets: dict[str, set[str]] = defaultdict(set)
        self.director_assets: dict[str, set[str]] = defaultdict(set)
        self.theme_assets: dict[str, set[str]] = defaultdict(set)
        self.language_assets: dict[str, set[str]] = defaultdict(set)
        self.type_assets: dict[str, set[str]] = defaultdict(set)
        self.actor_aliases = actor_aliases or []
        self.guarded_aliases: dict[str, list[Entity]] = defaultdict(list)
        self.lexicon_by_length: dict[int, list[str]] = defaultdict(list)
        for word in self.lexicon:
            self.lexicon_by_length[len(word)].append(word)
        self.genre_words: dict[str, set[str]] = defaultdict(set)         # word -> full genre norms
        self.genre_display: dict[str, str] = {}
        self.languages: dict[str, str] = {}
        self.max_words = 1
        self._build()
        self.partial_tri: dict[str, set[str]] = defaultdict(set)
        self.partial_skel: dict[str, set[str]] = defaultdict(set)
        for word in self._partials:
            for tri in trigrams(word):
                self.partial_tri[tri].add(word)
            self.partial_skel[skeleton(word)].add(word)
        self._coverage_cache = None
        for row in self.actor_aliases:
            if row['actor_norm'] not in self.actor_assets:
                continue
            phrase = row['alias_norm']
            # Exact real names/titles take precedence over somebody else's nickname.
            if any(not e.via_alias for e in self.exact.get(phrase, ())):
                continue
            entity = Entity('actor', row['actor_name'], row['actor_norm'], via_alias=True)
            if row['requires_context']:
                self.guarded_aliases[phrase].append(entity)
                self.max_words = max(self.max_words, len(phrase.split()))
            else:
                self._add(phrase, entity, True)

    # -- build ----------------------------------------------------------------------------
    def _add(self, phrase: str, ent: Entity, fuzzy: bool) -> None:
        if not phrase:
            return
        if ent not in self.exact[phrase]:
            self.exact[phrase].append(ent)
        self.max_words = max(self.max_words, len(phrase.split()))
        if fuzzy and ent.kind in FUZZY_KINDS:
            idx = len(self.fuzzy_keys)
            self.fuzzy_keys.append((phrase, ent))
            for t in trigrams(phrase):
                self.tri[t].add(idx)
            sk = skeleton(phrase)
            if ent not in self.skel[sk]:
                self.skel[sk].append(ent)

    def _build(self) -> None:
        langs: dict[str, str] = {}
        for a in self.assets.values():
            for language in a.audio_languages:
                langs[normalize(language)] = language
                self.language_assets[normalize(language)].add(a.asset_key)
            self.type_assets[a.content_type].add(a.asset_key)
            self.title_assets[a.title_norm].add(a.asset_key)
            te = Entity("title", a.title, a.title_norm)
            self.title_entities[a.title_norm] = te
            for word in set(a.title_norm.split()):
                self.title_words[word].add(a.title_norm)
            self._add(a.title_norm, te, True)
            bare = _strip_article(a.title_norm)
            if bare != a.title_norm and len(bare) >= 3:
                self._add(bare, Entity("title", a.title, a.title_norm), True)
                self.title_assets[bare].add(a.asset_key)
            for al in a.aliases:
                an = normalize(al)
                if an and an != a.title_norm:
                    self._add(an, Entity("title", a.title, a.title_norm, via_alias=True), True)
                    self.title_assets[an].add(a.asset_key)
            for name, nn in zip(a.cast_names, a.cast_norm):
                self.actor_assets[nn].add(a.asset_key)
                self._add(nn, Entity("actor", name, nn), True)
            for d in a.directors:
                dn = normalize(d)
                self.director_assets[dn].add(a.asset_key)
                self._add(dn, Entity("director", d, dn), True)
            chars = set(a.characters_norm)          # named roles from the catalog; scene-only extras stay free text
            by_norm = {normalize(c): c for c in a.characters}
            for cn in chars:
                disp = by_norm.get(cn, cn.title())
                self.character_assets[cn].add(a.asset_key)
                self._add(cn, Entity("character", disp, cn), True)
            for g, gn in zip(a.genres, a.genres_norm):
                self.genre_display[gn] = g
                self._add(gn, Entity("genre", g, gn), False)
                for w in gn.split():
                    if len(w) >= 3 and w not in NOISE:
                        self.genre_words[w].add(gn)
            for theme in a.themes:
                tn = normalize(theme)
                self.theme_assets[tn].add(a.asset_key)
                self._add(tn, Entity('theme', theme, tn), False)
        # partial actor/character names (one word of a multi-word name) -> only if unambiguous later
        self._partials: dict[str, set[Entity]] = defaultdict(set)
        for phrase, ents in list(self.exact.items()):
            for e in ents:
                if e.kind in ("actor", "director", "character") and " " in e.norm:
                    for p in e.norm.split():
                        if len(p) >= 4 and p not in NOISE and not p.isdigit() and p not in {'actor','actress','character','director'}:
                            self._partials[p].add(Entity(e.kind, e.canonical, e.norm, partial=True))
        for w, full in self.genre_words.items():
            if w not in self.exact or all(e.kind != "genre" for e in self.exact[w]):
                self.exact[w].append(Entity("genre", w, w))
        for key, canon in GENRE_SYNONYMS.items():
            # Observe every spelling in the equivalence group (e.g. Romantic Drama),
            # not only the canonical key. Multi-word genres must match as phrases:
            # science fiction must never inherit all Period Fiction titles.
            fulls = {g for g in self.genre_display if any(
                f" {normalize(syn)} " in f" {g} " for syn in canon)}
            self.genre_words[key].update(fulls)
            for syn in canon:
                normalized = normalize(syn)
                self.genre_words[normalized].update(fulls)
                # Register known genres even when absent: an explicit sci-fi filter
                # then honestly yields no matches rather than unrestricted retrieval.
                self.exact[normalized] = [e for e in self.exact.get(normalized, []) if e.kind != "genre"]
                self.exact[normalized].append(Entity("genre", key, key))
        for alias, canon in LANGUAGE_ALIASES.items():
            self.exact[alias].append(Entity("language", canon, normalize(canon)))
        for nl, disp in langs.items():
            if all(e.kind != "language" for e in self.exact.get(nl, [])):
                self.exact[nl].append(Entity("language", disp, nl))
        self.languages = {normalize(v): v for v in set(LANGUAGE_ALIASES.values()) | set(langs.values())}
        for word, ct in CONTENT_TYPE_WORDS.items():
            self.exact[word].append(Entity("content_type", ct, ct))
        self.max_words = max(self.max_words, 3, max((len(k.split()) for k in self.exact), default=1))

    # -- lookups --------------------------------------------------------------------------
    def entities_for(self, phrase: str) -> list[Entity]:
        ents = list(self.exact.get(phrase, ()))
        if not ents:
            sing = " ".join(singularize(w) for w in phrase.split())
            if sing != phrase:
                ents = [e for e in self.exact.get(sing, ()) if e.kind in ("genre", "content_type")]
        return ents

    def partial_entities(self, word: str) -> list[Entity]:
        return sorted(self._partials.get(word, ()), key=lambda e: (e.kind, e.norm))

    def _title_fragment(self, tokens: list[str], start: int, end: int, existing: list[Entity]) -> list[Entity]:
        """Indexed whole-word title fragments, guarded by request context.

        No movie-specific rules or full-catalog request scan. Query tokens select
        posting lists; only their intersection is checked for contiguous wording.
        Explicit person slots and known category requests retain their meaning.
        """
        if any(e.kind == 'title' for e in existing):
            return []  # Exact title always wins over longer partial title matches.
        words = tokens[start:end]
        phrase = ' '.join(words)
        before = tokens[start-1] if start else ''
        if before in EXPLICIT_PERSON_SLOTS:
            return []
        outside = tokens[:start]+tokens[end:]
        if any(t in NARRATIVE_TITLE_BLOCKERS for t in outside):
            return []
        explicit = any(t in TITLE_REQUEST_CUES for t in tokens[:start])
        if not explicit:
            if len(phrase) < 4 or not any(t.isalpha() for t in words):
                return []
            if any(e.kind in {'genre','theme','language','content_type'} for e in existing):
                return []
            significant = [w for w in words if w not in NOISE]
            # A known person name is already an entity candidate, not an ordinary
            # plot word. Its frequency must not let a character override a title.
            named = any(e.kind in {'actor','director','character'} for e in existing)
            common = any(self.df(w)/self.doc_total >= .05 and self.df(w) > len(self.title_words[w])
                         for w in significant)
            if not significant or (common and not named):
                return []
        # Remaining words must be request grammar or recognized structured facets,
        # rather than an ordinary plot description containing a title word.
        sort_words = {word for cue in SORT_LATEST+SORT_OLDEST for word in cue}
        fragment_indexes = set(range(start,end))
        covered = {i for i,t in enumerate(tokens) if i in fragment_indexes or t in NOISE
                   or t in CONTENT_TYPE_WORDS or t in CATALOG_MARKERS or t in TITLE_REQUEST_CUES or t in sort_words}
        for i in range(len(tokens)):
            if i in covered:
                continue
            for length in range(min(self.max_words,len(tokens)-i),0,-1):
                inds=set(range(i,i+length))
                if inds & fragment_indexes:
                    continue
                if any(e.kind in {'actor','director','character','genre','theme','language','content_type'}
                       for e in self.entities_for(' '.join(tokens[i:i+length]))):
                    covered |= inds
                    break
        if len(covered) != len(tokens):
            return []
        postings = [self.title_words.get(word,set()) for word in words]
        if not postings or any(not p for p in postings):
            return []
        matching = set.intersection(*sorted(postings,key=len))
        return [self.title_entities[n] for n in sorted(matching)
                if f' {phrase} ' in f' {n} ']

    def find_spans(self, tokens: list[str]) -> list[Span]:
        spans: list[Span] = []
        n = len(tokens)
        for i in range(n):
            for length in range(min(self.max_words, n - i), 0, -1):
                phrase = " ".join(tokens[i:i + length])
                guarded = self.guarded_aliases.get(phrase, [])
                before = tokens[i-1] if i else ''
                after = tokens[i+length] if i+length < n else ''
                if guarded and (before in {'starring','featuring','actor','actress','with'}
                                or after in CONTENT_TYPE_WORDS
                                or (i == 0 and length == n and length > 1)):
                    spans.append(Span(i, i+length, phrase, list(guarded)))
                ents = self.entities_for(phrase)
                fragments = (self._title_fragment(tokens,i,i+length,ents)
                             if all(w in self.title_words for w in tokens[i:i+length]) else [])
                if fragments:
                    spans.append(Span(i,i+length,phrase,fragments+ents,match='title_fragment'))
                    continue
                if ents:
                    spans.append(Span(i, i + length, phrase, ents))
                    continue
                sk = skeleton(phrase)
                if length >= 2 or len(phrase) >= 5:
                    sk_ents = self.skel.get(sk)
                    if sk_ents:
                        spans.append(Span(i, i + length, phrase, list(sk_ents), match="skeleton"))
        return spans

    def fuzzy(self, phrase: str, *, kinds: tuple[str, ...] = FUZZY_KINDS, max_dist: int = 2,
              limit: int = 5) -> list[Candidate]:
        """Candidates for a phrase that matched nothing exactly (best distance per entity)."""
        if len(phrase) < 3:
            return []
        # short strings tolerate fewer edits (one typo in a 4-letter word is a different word)
        max_dist = min(max_dist, 0 if len(phrase) <= 4 else 1 if len(phrase) <= 7 else 2)
        shared: Counter[int] = Counter()
        for t in trigrams(phrase):
            for idx in self.tri.get(t, ()):
                shared[idx] += 1
        sk = skeleton(phrase)
        best: dict[tuple[str, str], Candidate] = {}
        for idx, _ in shared.most_common(40):
            key, ent = self.fuzzy_keys[idx]
            if ent.kind not in kinds:
                continue
            d = osa_distance(phrase, key, max_dist)
            phon = skeleton(key) == sk
            if phon:
                d = min(d, 1)
            if d <= max_dist:
                cur = best.get(ent.ident)
                if cur is None or d < cur.distance:
                    best[ent.ident] = Candidate(ent, d, phon)
        for ent in self.skel.get(sk, ()):
            if ent.kind in kinds and ent.ident not in best:
                best[ent.ident] = Candidate(ent, 1, True)
        # partial (single word of a longer name)
        if " " not in phrase and len(phrase) >= 4:
            shared_partials = Counter()
            for tri in trigrams(phrase):
                shared_partials.update(self.partial_tri.get(tri, ()))
            candidates = {p for p,_ in sorted(shared_partials.items(), key=lambda item:(-item[1],item[0]))[:40]}
            candidates |= self.partial_skel.get(sk, set())
            for p in sorted(candidates):
                ents = self._partials[p]
                if abs(len(p) - len(phrase)) > max_dist:
                    continue
                d = osa_distance(phrase, p, max_dist)
                if skeleton(p) == sk:
                    d = min(d, 1)
                if d <= max_dist:
                    for e in ents:
                        if e.kind in kinds and e.ident not in best:
                            best[e.ident] = Candidate(e, d, skeleton(p) == sk)
        return sorted(best.values(), key=lambda c: (c.distance, KIND_PRIORITY[c.entity.kind], c.entity.norm))[:limit]

    # -- asset sets -----------------------------------------------------------------------
    def assets_for(self, ent: Entity) -> set[str]:
        k, n = ent.kind, ent.norm
        if k == "title":
            return set(self.title_assets.get(n, ()))
        if k == "actor":
            return set(self.actor_assets.get(n, ()))
        if k == "director":
            return set(self.director_assets.get(n, ()))
        if k == "character":
            return set(self.character_assets.get(n, ()))
        if k == "genre":
            fulls = self.genre_words.get(n, set()) | ({n} if n in self.genre_display else set())
            return {key for key, a in self.assets.items() if fulls & set(a.genres_norm)}
        if k == 'theme':
            return set(self.theme_assets.get(n, ()))
        if k == "language":
            return set(self.language_assets.get(n, ()))
        if k == "content_type":
            return set(self.type_assets.get(n, ()))
        return set()

    def genre_strings(self, ent: Entity) -> set[str]:
        return self.genre_words.get(ent.norm, set()) | ({ent.norm} if ent.norm in self.genre_display else set())

    # -- stats ---------------------------------------------------------------------------
    def coverage(self) -> dict[str, float]:
        if self._coverage_cache is not None:
            return self._coverage_cache
        n = max(len(self.assets), 1)
        self._coverage_cache = {
            "release_year": sum(a.release_year is not None for a in self.assets.values()) / n,
            "release_date": sum(a.release_date is not None for a in self.assets.values()) / n,
            "platform_release_date": sum(a.platform_release_date is not None for a in self.assets.values()) / n,
            "content_type": sum(a.content_type is not None for a in self.assets.values()) / n,
            "directors": sum(bool(a.directors) for a in self.assets.values()) / n,
        }
        return self._coverage_cache

    def df(self, word: str) -> int:
        return self.lexicon.get(word, 0)

    def knows_word(self, word: str) -> bool:
        return word in self.lexicon

    def summary(self) -> dict[str, Any]:
        return {"assets": len(self.assets), "entities": sum(len(v) for v in self.exact.values()),
                "lexicon_words": len(self.lexicon), "coverage": self.coverage(),
                "generation": self.generation.get("generation_id")}


def asset_from_row(row: dict[str, Any], aliases: list[str] | None = None) -> AssetInfo:
    return AssetInfo(asset_key=row["show_id"], asset_id=row["show_id"],
        audio_languages=list(row["audio_languages"]), themes=list(row["themes"]), title=row["title"],
        title_norm=row["title_norm"], description=row["plot_summary"], genres=list(row["genres"]),
        genres_norm=list(row["genres_norm"]), cast_names=list(row["cast_names"]), cast_norm=list(row["cast_norm"]),
        characters=list(row["character_names"]), characters_norm=list(row["character_norm"]),
        directors=list(row["directors"]), content_type=row["content_type"],
        release_year=row["release_date"].year if row["release_date"] else None,
        release_date=row["release_date"], platform_release_date=row["platform_release_date"],
        thumbnail_image_url=(row["source_metadata"].get("thumbnail_image_url") or row["source_metadata"].get("thumbnail_url")))
