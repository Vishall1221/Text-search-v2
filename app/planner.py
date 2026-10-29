"""Constraint-first query planner (deterministic; no LLM).

``plan_query(raw, catalog, today)`` turns one standalone English query into a ``Plan``:

  resolved entities -> hard filters (AND across facets, OR within a facet)
  negations, years, sort, top-N, popularity (reported as unavailable)
  request type (title | similar | catalog | scene | quote | browse | mixed)
  residual text for the lexical and dense channels
  spelling decisions: unique single-edit autocorrect, otherwise <=3 suggestions + clarification

Everything the plan did is recorded in ``trace`` and ``notes`` so the response can explain itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.dates import YearRange, extract_years
from app.gazetteer import KIND_PRIORITY, Candidate, Catalog, Entity, Span
from app.operators import Operators, extract_operators, negation_cue_at
from app.text import fold, normalize, osa_distance, scrub_pii, singularize
from app.vocab import (ACTOR_CUES, AMBIGUOUS_CONTENT_WORDS, AMBIGUOUS_NEXT_OK, BY_CUES, CATALOG_MARKERS, CATALOG_PHRASES, COMMAND_WORDS, CONNECTORS,
                       CONTENT_TYPE_WORDS, DIALOGUE_MARKERS, DIRECTOR_CUES, FUNCTION_WORDS,
                       GENRE_SCENE_FOLLOWERS, LANGUAGE_NON_FILTER_FOLLOWERS, NOISE, SCENE_MARKERS,
                       SHOW_NOUN_PRECEDERS, SHOW_VERB_FOLLOWERS, SIMILAR_CUES)
from config.settings import settings

COMMA = "commasep"
QUOTE_TOKEN = "quotedphrase"
SPEECH_VERBS = {"says", "said", "saying", "speaks", "shouts", "whispers", "asks", "replies"}
GREETINGS = {"hi", "hello", "hey", "hii", "heya", "namaste", "yo", "thanks", "thank", "you", "bye", "goodbye",
             "ok", "okay", "cool", "good", "morning", "evening", "afternoon", "how", "are", "who", "what", "can",
             "do", "help", "is", "this", "your", "name", "please", "sup"}
NEG_FILLER = {"the", "a", "an", "any", "of", "movies", "movie", "films", "film", "shows", "series", "scenes", "scene",
              "ones", "one", "in", "language", "languages", "titles", "genre", "genres", "very", "too", "really"}
PERSON_CUE_WORDS = ACTOR_CUES | DIRECTOR_CUES | BY_CUES
# Cues that make a person slot an explicit request. "stars", "features", "cast" are usually narrative.
EXPLICIT_PERSON_CUES = {"starring", "starred", "featuring", "actor", "actress", "director", "directed"}
NARRATIVE_DETERMINERS = {"the", "a", "an", "his", "her", "their", "its", "my", "our", "your", "this", "that", "co"}
CONNECTOR_TOKENS = CONNECTORS | {COMMA}
MARKER_TOKENS = (SCENE_MARKERS | CATALOG_MARKERS | DIALOGUE_MARKERS)
RESIDUAL_SKIP = NOISE | CONNECTOR_TOKENS | MARKER_TOKENS | {"movie", "movies", "film", "films", "series", "show", "shows"}
FIXABLE_WORDS = set(CONTENT_TYPE_WORDS) | SCENE_MARKERS | COMMAND_WORDS


# ---------------------------------------------------------------------------------------
@dataclass
class Facet:
    alternatives: list[Entity]
    negated: bool = False
    surface: str = ""

    @property
    def kind(self) -> str:
        return min((e.kind for e in self.alternatives), key=lambda k: KIND_PRIORITY[k])

    def label(self) -> str:
        names = " / ".join(dict.fromkeys(e.canonical for e in self.alternatives))
        return f"{'NOT ' if self.negated else ''}{self.kind}: {names}"


@dataclass
class Correction:
    original: str
    corrected: str
    kind: str
    method: str                      # edit | transliteration | partial


@dataclass
class Clarification:
    reason: str                      # unknown_name | ambiguous_name | empty_query | chitchat
    message: str
    suggestions: list[str] = field(default_factory=list)
    preview_query: str | None = None


@dataclass
class ChannelSpec:
    name: str
    scope: str                       # catalog | scene
    kinds: tuple[str, ...]           # document kinds searched
    method: str                      # lex | phrase | dense
    weight: float


@dataclass
class Plan:
    raw: str
    scrubbed: str
    normalized: str
    mode: str = "mixed"              # title | similar | catalog | scene | quote | browse | mixed | clarify
    facets: list[Facet] = field(default_factory=list)
    years: YearRange | None = None
    sort: str | None = None
    limit: int | None = None
    terms: list[str] = field(default_factory=list)          # lexical terms (rarest first)
    dense_text: str = ""
    phrase: str | None = None
    phrase_quoted: bool = False
    similar_to: list[str] = field(default_factory=list)     # asset keys
    allowed: set[str] | None = None                          # asset keys that satisfy every filter
    ignored_negations: list[str] = field(default_factory=list)
    corrections: list[Correction] = field(default_factory=list)
    clarification: Clarification | None = None
    notes: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    pii_scrubbed: bool = False
    approximate: bool = False
    scene_actor_terms: list[str] = field(default_factory=list)   # actor/character norms for in-scene ordering
    empty_reason: str | None = None

    def has_filters(self) -> bool:
        return bool(self.facets) or self.years is not None


# ---------------------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------------------
_QUOTE = re.compile(r'"([^"]{2,240})"')


class QuotedPhrases(list[str]):
    """Quote values with an out-of-band map of placeholders created for this query."""
    def __init__(self) -> None:
        super().__init__()
        self.tokens: dict[str, int] = {}
        self.spoken_text: str | None = None


def prepare(raw: str) -> tuple[str, bool, str, list[str], list[str]]:
    """-> (scrubbed, pii_hit, normalised text, tokens, quoted phrases)."""
    scrubbed, pii = scrub_pii(raw or "")
    # the redaction tags ("[phone]") are for logs and notes only; they must not become search words
    text = fold(re.sub(r"\[(?:email|id|phone)\]", " ", scrubbed))
    quoted = QuotedPhrases()
    original_tokens = set(normalize(text).split())
    if not _QUOTE.search(text):
        spoken = re.search(r"\b(?:" + "|".join(sorted(SPEECH_VERBS)) + r")\s+(.+)$", text)
        if spoken:
            quoted.spoken_text = spoken.group(1).strip()

    def stash(m: re.Match[str]) -> str:
        # PostgreSQL's simple configuration must see the same punctuation as the
        # indexed dialogue; normalize() would turn can't into the unrelated cant.
        quoted.append(fold(m.group(1)).strip())
        index = len(quoted) - 1
        serial = 0
        placeholder = f"{QUOTE_TOKEN}{index}slot{serial}"
        while placeholder in original_tokens:
            serial += 1
            placeholder = f"{QUOTE_TOKEN}{index}slot{serial}"
        quoted.tokens[placeholder] = index
        return f" {placeholder} "

    text = _QUOTE.sub(stash, text)
    text = re.sub(r"\bsci[\s-]?fi\b", "scifi", text)
    text = text.replace(",", f" {COMMA} ").replace(";", f" {COMMA} ")
    norm = normalize(text)
    tokens = norm.split()
    # "mmovie" -> "movie": undo a doubled first letter only when the result is a known operator word
    fixed = []
    for t in tokens:
        if len(t) >= 5 and t[0] == t[1] and t[1:] in FIXABLE_WORDS:
            t = t[1:]
        fixed.append(t)
    return scrubbed, pii, norm, fixed, quoted


def _quote_index(token: str, quoted: list[str]) -> int | None:
    return quoted.tokens.get(token) if isinstance(quoted, QuotedPhrases) else None


# ---------------------------------------------------------------------------------------
# Span selection
# ---------------------------------------------------------------------------------------

def _is_show_verb(tokens: list[str], i: int) -> bool:
    """'show me ...' is a command; 'a show about ...' / 'tamil shows' is a content type."""
    if tokens[i] != "show":
        return False
    prev = tokens[i - 1] if i > 0 else ""
    nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
    if nxt in SHOW_VERB_FOLLOWERS:
        return True
    return prev not in SHOW_NOUN_PRECEDERS and i == 0 or (i == 0 and nxt not in {"about", "where", "with"})


def select_spans(tokens: list[str], catalog: Catalog, whole_query_ok: bool,
                 tracks: list[tuple[int, int]] | None = None) -> list[Span]:
    cands = catalog.find_spans(tokens)
    cleaned: list[Span] = []
    n = len(tokens)
    facet_starts = {c.start for c in cands if any(e.kind != "content_type" for e in c.entities)}
    for sp in cands:
        ents = sp.entities
        if sp.length == 1:
            tok = tokens[sp.start]
            if tok in NOISE and not whole_query_ok:
                continue
            if tok.startswith(QUOTE_TOKEN) or tok in (COMMA,) or tok.isdigit():
                continue
            if tok in ("show", "shows") and _is_show_verb(tokens, sp.start):
                continue
        nxt = tokens[sp.end] if sp.end < n else ""
        if (sp.length == 1 and tokens[sp.start] in AMBIGUOUS_CONTENT_WORDS and all(e.kind == "content_type" for e in ents)
                and sp.end < n and nxt not in AMBIGUOUS_NEXT_OK and nxt not in CONNECTOR_TOKENS
                and sp.end not in facet_starts):
            continue                                  # "serial murders", "series of events": ordinary English
        # genre / content-type word describing a scene ("action scene") is content, not a filter
        kept = [e for e in ents if not (e.kind in ("genre",) and nxt in GENRE_SCENE_FOLLOWERS)]
        # "English subtitles", "Hindi dubbed" describe a track, not the movie language
        if tracks is not None and any(e.kind == "language" for e in kept) and nxt in LANGUAGE_NON_FILTER_FOLLOWERS:
            tracks.append((sp.start, sp.end + 1))
        kept = [e for e in kept if not (e.kind == "language" and nxt in LANGUAGE_NON_FILTER_FOLLOWERS)]
        # A genre the catalog does not have is an honest empty filter only in a listing ("adventure movies").
        # In a description ("an adventure in the jungle") the word is ordinary searchable text.
        if any(e.kind == "genre" for e in kept) and not any(t in CONTENT_TYPE_WORDS for t in tokens):
            kept = [e for e in kept if not (e.kind == "genre" and not catalog.assets_for(e))]
        if any(e.kind == "title" for e in kept):
            outside = tokens[:sp.start] + tokens[sp.end:]
            request_only = all(t in NOISE | MARKER_TOKENS | CONNECTOR_TOKENS or t in CONTENT_TYPE_WORDS
                               or t.isdigit() for t in outside)
            explicit = sp.start > 0 and tokens[sp.start - 1] in {"in", "from", "like", "named", "called", "titled"}
            # A title of two or more rare words ("Raktha Vajram", "Kaattu Pey") is unmistakable even inside a
            # longer request ("Raktha Vajram climax fight"); only common-word titles ("The Gift") need context.
            words = [w for w in sp.phrase.split() if w not in {"the", "a", "an"}]
            distinctive = len(words) >= 2 and all(
                catalog.df(w) / catalog.doc_total < 0.05 for w in words if not w.isdigit())
            if not request_only and not explicit and not distinctive:
                kept = [e for e in kept if e.kind != "title"]
        if not kept:
            continue
        sp.entities = kept
        cleaned.append(sp)
    order = sorted(cleaned, key=lambda s: (-s.length, min(KIND_PRIORITY[e.kind] for e in s.entities),
                                           s.match != "exact", s.start))
    taken: list[Span] = []
    used: set[int] = set()
    for sp in order:
        rng = set(range(sp.start, sp.end))
        if rng & used:
            continue
        # a skeleton (transliteration) match must not hide an exact shorter match inside it
        taken.append(sp)
        used |= rng
    return sorted(taken, key=lambda s: s.start)


# ---------------------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------------------
def plan_query(raw: str, catalog: Catalog, today: date | None = None, *, _depth: int = 0) -> Plan:
    today = today or date.today()
    scrubbed, pii, norm, tokens, quoted = prepare(raw)
    plan = Plan(raw=raw, scrubbed=scrubbed, normalized=norm, pii_scrubbed=pii)
    if pii:
        plan.notes.append("Personal details were removed from the query before searching.")

    # --- trivial / conversational input ---------------------------------------------------
    letters = re.sub(r"[^A-Za-z]", "", norm)
    if len(letters) < 2 and not quoted:
        plan.mode = "clarify"
        plan.clarification = Clarification("empty_query", "Tell me what to look for - a title, an actor, a genre, or "
                                           "a scene description.")
        return plan
    if tokens and all(t in GREETINGS for t in tokens) and not catalog.entities_for(" ".join(tokens)):
        plan.mode = "clarify"
        plan.clarification = Clarification(
            "chitchat", "I search the catalog. Try a title, an actor, a genre, or describe a scene "
                        "(for example: \"scenes with a courtroom argument\").")
        return plan

    corrections: list[Correction] = []
    for _ in range(4):                               # each pass applies at most one autocorrection
        result = _analyze(tokens, quoted, catalog, plan, today)
        if isinstance(result, Clarification):
            plan.clarification = result
            plan.mode = "clarify"
            if result.preview_query and _depth == 0:
                plan.trace.append(f"preview query: {result.preview_query}")
            plan.corrections = corrections
            return plan
        if isinstance(result, tuple):                # (new tokens, correction)
            tokens, corr = result
            corrections.append(corr)
            continue
        break
    plan.corrections = corrections
    for c in corrections:
        plan.trace.append(f"autocorrect {c.kind} '{c.original}' -> '{c.corrected}' ({c.method})")
    if corrections:
        plan.approximate = True
        plan.notes.append("Showing results for " + ", ".join(f"\"{c.corrected}\" (you typed \"{c.original}\")"
                                                            for c in corrections) + ".")
    _finalize(plan, catalog)
    return plan


def _analyze(tokens: list[str], quoted: list[str], catalog: Catalog, plan: Plan, today: date):
    """Fill ``plan`` from tokens. Returns None when done, a (tokens, Correction) to retry with a
    corrected spelling, or a Clarification."""
    plan.facets, plan.years, plan.sort, plan.limit = [], None, None, None
    plan.terms, plan.dense_text, plan.phrase, plan.phrase_quoted = [], "", None, False
    plan.similar_to, plan.ignored_negations, plan.notes, plan.trace = [], [], [n for n in plan.notes if "Personal" in n], []
    plan.scene_actor_terms, plan.approximate = [], False
    n = len(tokens)
    whole = [t for t in tokens if t not in NOISE and t not in CONNECTOR_TOKENS]

    tracks: list[tuple[int, int]] = []
    spans = select_spans(tokens, catalog, whole_query_ok=len(tokens) <= 3, tracks=tracks)
    consumed: set[int] = set()
    for sp in spans:
        consumed |= set(range(sp.start, sp.end))
    for a, b in tracks:                       # "english subtitles", "hindi dubbed": track, not language
        consumed |= set(range(a, b))
        plan.approximate = True
        plan.notes.append(f"Audio/subtitle options (\"{' '.join(tokens[a:b])}\") are not searchable; ignored.")
    # quoted phrase placeholders
    for i, t in enumerate(tokens):
        if _quote_index(t, quoted) is not None:
            consumed.add(i)
    # words spoken after "says ..." are a literal phrase: never names, negations or operators
    phrase_region: list[str] = []
    for i, t in enumerate(tokens):
        if t in SPEECH_VERBS and i not in consumed and t not in (COMMA,):
            tail = [x for x in tokens[i + 1:] if x != COMMA]
            if len([x for x in tail if x not in ("that", "the", "words", "line", "like")]) >= 2:
                phrase_region = tokens[i + 1:]
                consumed.add(i)
                consumed |= set(range(i + 1, n))
                spans = [sp for sp in spans if sp.end <= i]
                break
    # years, then sort / top-N / popularity
    years, ysed = extract_years(tokens, consumed, today)
    consumed |= ysed
    entity_starts = {sp.start for sp in spans if any(e.kind in ("genre", "language", "content_type", "actor", "character", "director") for e in sp.entities)}
    ops = extract_operators(tokens, consumed, entity_starts)
    consumed |= ops.used
    plan.years, plan.sort, plan.limit = years, ops.sort, ops.limit
    if ops.popularity:
        plan.approximate = True
        plan.notes.append(f"\"{ops.popularity}\" cannot be applied: the catalog has no popularity or rating data. "
                          "Results are ranked by relevance.")

    category_spelling = _resolve_category_typo(tokens, consumed, spans, catalog)
    if category_spelling is not None:
        return category_spelling

    # --- negation scopes ------------------------------------------------------------------
    negated_spans: set[int] = set()
    neg_free: list[str] = []
    i = 0
    while i < n:
        k = 0 if i in consumed else negation_cue_at(tokens, i)
        if not k:
            i += 1
            continue
        for j in range(i, i + k):
            consumed.add(j)
        j = i + k
        while j < n and tokens[j] in NEG_FILLER and not any(sp.start == j for sp in spans):
            consumed.add(j); j += 1
        # "not starring X" / "without actor X" / "not directed by X": the person cue belongs to the name
        m = j
        while m < n and tokens[m] in PERSON_CUE_WORDS and not any(sp.start == m for sp in spans):
            m += 1
        if m > j and any(sp.start == m for sp in spans):
            consumed.update(range(j, m)); j = m
        while j < n:
            hit = next((sp for sp in spans if sp.start == j), None)
            if hit is not None:
                # A genre mentioned in a scene exclusion describes scene content,
                # not the whole film's catalog genre ("ghost but not romance").
                scene_exclusion = (all(e.kind == "genre" for e in hit.entities)
                                   and any(t in SCENE_MARKERS for t in tokens[:i]))
                if scene_exclusion:
                    neg_free.extend(tokens[hit.start:hit.end])
                    spans = [sp for sp in spans if sp is not hit]
                else:
                    negated_spans.add(hit.start)
                j = hit.end
            elif tokens[j] not in RESIDUAL_SKIP and j not in consumed and not neg_free_blocked(tokens, i, k, bool(neg_free)):
                neg_free.append(tokens[j]); consumed.add(j); j += 1
            else:
                break
            if j < n and tokens[j] in CONNECTOR_TOKENS and j + 1 < n and negation_cue_at(tokens, j + 1):
                consumed.add(j); j += 1          # "... and no romance": a new exclusion; the outer loop takes it
                break
            if j < n and tokens[j] in CONNECTOR_TOKENS and j + 1 < n and (
                    any(sp.start == j + 1 for sp in spans) or tokens[j + 1] not in RESIDUAL_SKIP):
                consumed.add(j); j += 1
                continue
            break
        i = j
    if neg_free:
        plan.ignored_negations = neg_free
        plan.approximate = True
        plan.notes.append("Exclusion of \"" + " ".join(neg_free) + "\" is not enforced for free-text terms; it was "
                          "left out of the search so it does not attract matches.")

    # --- person cues ("starring X", "directed by X") are consumed with the name ----------------------
    cues: dict[int, str] = {}
    cue_words = (ACTOR_CUES | DIRECTOR_CUES) - {"with"}
    for sp in spans:
        prev = tokens[sp.start - 1] if sp.start > 0 else ""
        prev2 = tokens[sp.start - 2] if sp.start > 1 else ""
        if prev in cue_words:
            cues[sp.start] = "director" if prev in DIRECTOR_CUES else "actor"
            consumed.add(sp.start - 1)
        elif prev in BY_CUES and prev2 in DIRECTOR_CUES:
            cues[sp.start] = "director"
            consumed.update({sp.start - 1, sp.start - 2})
    no_director_data = catalog.coverage()["directors"] == 0
    if no_director_data:                       # "directed by <name>" cannot be applied: say so, drop the words
        for i, t in enumerate(tokens):
            if t in DIRECTOR_CUES and i not in consumed and not (i > 0 and tokens[i - 1] in NARRATIVE_DETERMINERS):
                j = i + 1
                if j < n and tokens[j] in BY_CUES:
                    j += 1
                names = []
                while j < n and len(names) < 3 and j not in consumed and tokens[j] not in RESIDUAL_SKIP:
                    names.append(tokens[j]); j += 1
                consumed.update(range(i, j))
                plan.approximate = True
                plan.notes.append("Director information is not available in this catalog, so \"directed by"
                                  + (f" {' '.join(names)}" if names else "") + "\" was not applied.")
    # --- facets (AND across; OR within) ------------------------------------------------------
    facets: list[Facet] = []
    prev_span: Span | None = None
    previous_kind: dict[str, tuple[int, Span]] = {}
    for sp in spans:
        ent = _pick_entities(sp, cues.get(sp.start))
        neg = sp.start in negated_spans
        kind = _kind_of(ent)
        prior = previous_kind.get(kind)
        facet_index = prior[0] if prior else len(facets) - 1
        prior_span = prior[1] if prior else prev_span
        joiner = _joiner(tokens, prior_span, sp) if prior_span is not None else None
        same_clause = False
        if facets and prior_span is not None and facets[facet_index].negated == neg and joiner is not None:
            last = facets[facet_index]
            if last.kind == _kind_of(ent):
                if joiner == "or" or _kind_of(ent) in ("language", "content_type", "title") or neg:
                    same_clause = True
        if same_clause:
            facets[facet_index].alternatives.extend(e for e in ent if e not in facets[facet_index].alternatives)
            facets[facet_index].surface += f" {sp.phrase}"
        else:
            facets.append(Facet(list(ent), neg, sp.phrase))
            facet_index = len(facets) - 1
        previous_kind[kind] = (facet_index, sp)
        if sp.match == "skeleton":
            plan.trace.append(f"transliteration match '{sp.phrase}' -> {ent[0].canonical}")
        prev_span = sp
    # --- spelling ----------------------------------------------------------------------------
    unresolved = _unresolved_runs(tokens, consumed, catalog)
    residual_len = sum(1 for i, t in enumerate(tokens) if i not in consumed and t not in RESIDUAL_SKIP)
    for sp in spans:
        if sp.match == "skeleton":
            ents = {e.ident for e in sp.entities}
            if len(ents) > 1 or len(sp.phrase) < settings.autocorrect_min_length:
                return _clarify_from(sp.phrase, [Candidate(e, 1, True) for e in sp.entities], tokens, sp.start, sp.end)
            only = sp.entities[0]
            return (tokens[:sp.start] + only.norm.split() + tokens[sp.end:],
                    Correction(sp.phrase, only.canonical, only.kind, "transliteration"))
    for (a, b) in unresolved:
        res = _resolve_run(tokens, a, b, catalog, residual_len)
        if res is not None:
            return res

    # An explicit, unresolved cast/director slot is still a hard constraint.
    # It must not become a semantic description that searches every movie.
    for i, token in enumerate(tokens):
        if token not in EXPLICIT_PERSON_CUES:
            continue
        # "the actress cries", "the director yells", "his co-star": a narrative noun, not a request slot
        if i > 0 and tokens[i - 1] in NARRATIVE_DETERMINERS:
            continue
        start = i + 1
        if start < n and tokens[start] in BY_CUES:
            start += 1
        if i in consumed and (start >= n or start in consumed):
            continue
        end = start
        while end < n and end not in consumed and tokens[end] not in CONNECTOR_TOKENS | {"about", "where", "before", "after"}:
            end += 1
        if end > start and (tokens[start] in NARRATIVE_DETERMINERS or tokens[start] in FUNCTION_WORDS
                            or end - start > 4):
            continue                          # "featuring a haunted house": a description, not a person
        if end > start:
            result = _resolve_run(tokens, start, end, catalog, end - start)
            if result is not None:
                return result
            phrase = " ".join(tokens[start:end])
            return Clarification("unknown_name", f'I could not find the requested {"director" if token in DIRECTOR_CUES else "actor"} "{phrase}" in this catalog. Check the name or try another person.')
        if token in {"starring", "featuring", "directed"}:
            return Clarification("unknown_name", "Which actor or director should I look for?")

    plan.facets = facets
    for f in facets:
        plan.trace.append(f.label())
    for f in facets:
        if f.negated:
            continue
        for e in f.alternatives:
            if e.kind in ("actor", "character"):
                plan.scene_actor_terms.append(e.norm)

    # --- markers, phrase, residual text -------------------------------------------------------
    marker_idx = {i for i, t in enumerate(tokens) if i not in consumed and t in MARKER_TOKENS}
    has_scene = any(tokens[i] in (SCENE_MARKERS - DIALOGUE_MARKERS) for i in marker_idx)
    has_dialogue = any(tokens[i] in DIALOGUE_MARKERS for i in marker_idx)
    has_catalog = any(tokens[i] in CATALOG_MARKERS for i in marker_idx) or any(re.search(p, " ".join(tokens)) for p in CATALOG_PHRASES)
    phrase_tokens: list[str] = []
    for i, t in enumerate(tokens):
        idx = _quote_index(t, quoted)
        if idx is not None:
            phrase_tokens = quoted[idx].split()
            plan.phrase_quoted = True
            has_dialogue = True
            break
    if not phrase_tokens and phrase_region:
        raw_spoken = quoted.spoken_text if isinstance(quoted, QuotedPhrases) else None
        tail = raw_spoken.split() if raw_spoken else [x for x in phrase_region if x != COMMA]
        while tail and tail[0] in {"that", "the", "words", "line", "like"}:
            tail = tail[1:]
        phrase_tokens = tail
    if phrase_tokens:
        plan.phrase = " ".join(phrase_tokens)

    consumed |= marker_idx
    similar = _similar_cue(tokens, consumed)
    if similar is not None:
        consumed |= set(range(*similar))
    content = [t for i, t in enumerate(tokens) if i not in consumed and t not in RESIDUAL_SKIP and not t.startswith(QUOTE_TOKEN)
               and not t.isdigit() or (i not in consumed and t.isdigit() and not t.startswith(QUOTE_TOKEN) and len(t) <= 4 and False)]
    content = list(dict.fromkeys(content))
    dense_tokens = [t for i, t in enumerate(tokens) if i not in consumed and t not in COMMAND_WORDS
                    and t not in CONNECTOR_TOKENS and not t.startswith(QUOTE_TOKEN)]
    while dense_tokens and dense_tokens[0] in FUNCTION_WORDS | {"movie", "movies", "film", "films"}:
        dense_tokens.pop(0)
    while dense_tokens and dense_tokens[-1] in FUNCTION_WORDS:
        dense_tokens.pop()
    plan.terms = sorted(content, key=lambda w: (catalog.df(w) or 10 ** 9) if catalog.knows_word(w) else 0)[:8]
    plan.dense_text = " ".join(dense_tokens) if content or plan.phrase else ""
    if similar is not None:
        plan.dense_text = ""

    # --- mode -----------------------------------------------------------------------------------
    title_facets = [f for f in facets if f.kind == "title" and not f.negated]
    if similar is not None and title_facets:
        plan.mode = "similar"
    elif plan.phrase or has_dialogue:
        plan.mode = "quote"
    elif title_facets and not content and not has_scene and not has_catalog:
        plan.mode = "title"
    elif has_scene:
        plan.mode = "scene"
    elif title_facets and content:
        plan.mode = "catalog" if has_catalog else "scene"
    elif has_catalog:
        plan.mode = "catalog"
    elif not content:
        plan.mode = "browse"
    else:
        plan.mode = "mixed"
    if plan.mode in ("scene", "catalog", "mixed") and not content and not plan.phrase:
        # nothing left to search for inside the documents: a named title is a title request
        plan.mode = "title" if title_facets else "browse"
    plan.trace.append(f"mode={plan.mode} terms={plan.terms} dense='{plan.dense_text}'")
    return None


def neg_free_blocked(tokens: list[str], i: int, k: int, after_exclusion: bool = False) -> bool:
    """'not' / 'no' before a plain word is ordinary narrative ("does not give up"); only the
    explicit exclusion cues can negate free text."""
    cue = tokens[i:i + k]
    if cue not in (["not"], ["no"], ["nor"]):
        return False
    prev = tokens[i - 1] if i > 0 else ""
    # "scenes with no ghost" excludes; so does "... and no romance" once an exclusion has started.
    # "he has no money and no hope" is narrative and keeps its words.
    return not (prev in {"with", "having"} or (prev in {"and", "or", COMMA} and after_exclusion))


def _kind_of(ents: list[Entity]) -> str:
    return min((e.kind for e in ents), key=lambda k: KIND_PRIORITY[k])


PERSON_KINDS = ("actor", "director", "character")


def _pick_entities(sp: Span, cue: str | None = None) -> list[Entity]:
    """Entities of the best-priority kind. Person names keep actor / director / character as
    alternatives, narrowed by an explicit cue ("directed by", "starring")."""
    ents = sorted(sp.entities, key=lambda e: (KIND_PRIORITY[e.kind], e.norm))
    if cue:
        narrowed = [e for e in ents if e.kind == cue or (cue == "actor" and e.kind == "character")]
        if narrowed:
            ents = narrowed
    best = ents[0].kind
    if best in PERSON_KINDS:
        return [e for e in ents if e.kind in PERSON_KINDS]
    out: list[Entity] = []
    for e in ents:
        if e.kind == best and e not in out:
            out.append(e)
    return out


def _joiner(tokens: list[str], a: Span, b: Span) -> str | None:
    gap = tokens[a.end:b.start]
    if not gap:
        return "adjacent"
    gap = [g for g in gap if g not in NEG_FILLER or g in ("the",)]
    if all(g in CONNECTOR_TOKENS or g in {"the", "with", "starring", "featuring"} for g in gap):
        if "or" in gap or "nor" in gap:
            return "or"
        return "and"
    return None


def _similar_cue(tokens: list[str], consumed: set[int]) -> tuple[int, int] | None:
    for seq in sorted(SIMILAR_CUES, key=len, reverse=True):
        L = len(seq)
        for i in range(len(tokens) - L + 1):
            if tuple(tokens[i:i + L]) == seq and not any(j in consumed for j in range(i, i + L)):
                # "like" alone only counts directly before a title span
                if seq == ("like",) and not (i + 1 < len(tokens) and (i + 1) in consumed):
                    continue
                return (i, i + L)
    return None


# ---------------------------------------------------------------------------------------
# Spelling
# ---------------------------------------------------------------------------------------
def _resolve_category_typo(tokens: list[str], consumed: set[int], spans: list[Span], catalog: Catalog):
    """A single uncertain category word in an explicit listing, never narrative prose.

    Reuse the normal single-edit/minimum-length auto-correction policy. Short or
    ambiguous matches ask for clarification instead. Exact corpus words win.
    """
    if not any(any(e.kind == "content_type" for e in sp.entities) for sp in spans):
        return None
    if any(t in SCENE_MARKERS | CATALOG_MARKERS | {"about", "where", "describing"} for t in tokens):
        return None
    negation_words = {"not", "no", "without", "except", "excluding", "exclude", "minus", "nor"}
    unknown_positions = [i for i, t in enumerate(tokens)
                         if i not in consumed and t not in RESIDUAL_SKIP | negation_words and not t.isdigit()]
    if len(unknown_positions) != 1:
        return None
    i = unknown_positions[0]
    word = tokens[i]
    if not 4 <= len(word) <= 24 or catalog.knows_word(word):
        return None
    candidates = []
    for alias, entities in catalog.exact.items():
        if " " in alias or abs(len(alias) - len(word)) > 1:
            continue
        distance = osa_distance(word, alias, 1)
        if distance != 1:
            continue
        candidates.extend(Candidate(e, distance, False) for e in entities if e.kind in {"genre", "language"})
    candidates = _unique(candidates)
    if not candidates:
        return None
    if (len(candidates) == 1 and len(word) >= settings.autocorrect_min_length
            and settings.autocorrect_max_edits >= 1):
        e = candidates[0].entity
        return (tokens[:i] + e.norm.split() + tokens[i + 1:], Correction(word, e.canonical, e.kind, "edit"))
    return _clarify_from(word, candidates, tokens, i, i + 1, ambiguous=len(candidates) > 1,
                         clear_best=len(candidates) == 1)


def _unresolved_runs(tokens: list[str], consumed: set[int], catalog: Catalog) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = None
    for i, t in enumerate(tokens + [""]):
        free = (i < len(tokens) and i not in consumed and t not in RESIDUAL_SKIP and not t.isdigit()
                and not t.startswith(QUOTE_TOKEN) and len(t) >= 3)
        if free and start is None:
            start = i
        elif not free and start is not None:
            runs.append((start, i))
            start = None
    # titles carry numbers ("Bambai 90", "Kaithi 2"): digits touching a run belong to it
    n = len(tokens)
    out: list[tuple[int, int]] = []
    for a, b in runs:
        while a > 0 and tokens[a - 1].isdigit() and (a - 1) not in consumed:
            a -= 1
        while b < n and tokens[b].isdigit() and b not in consumed:
            b += 1
        out.append((a, b))
    return out


def _unique(cands: list[Candidate]) -> list[Candidate]:
    out: dict[tuple[str, str], Candidate] = {}
    for c in cands:
        cur = out.get(c.entity.ident)
        if cur is None or c.distance < cur.distance:
            out[c.entity.ident] = c
    return sorted(out.values(), key=lambda c: (c.distance, KIND_PRIORITY[c.entity.kind], c.entity.norm))


def _resolve_run(tokens: list[str], a: int, b: int, catalog: Catalog, residual_len: int):
    """Try windows of the unresolved run (longest first). Returns a rewrite, a Clarification, or None."""
    cue_before = a > 0 and tokens[a - 1] in (ACTOR_CUES | DIRECTOR_CUES | BY_CUES | {"named", "called", "titled"})
    if residual_len > 4 and not cue_before:          # descriptive text: never rewrite words into names
        return None
    for size in range(min(4, b - a), 0, -1):
        for s in range(a, b - size + 1):
            words = tokens[s:s + size]
            phrase = " ".join(words)
            # A correctly spelled title the context guard set aside must not come back as "did you mean <itself>?"
            cands = [c for c in _unique(catalog.fuzzy(phrase, max_dist=2))
                     if not (c.distance == 0 and c.entity.kind == "title" and not c.entity.partial)]
            if not cands:
                continue
            unknown = [w for w in words if not catalog.knows_word(w)]
            best = cands[0]
            # the same string as a title and as a character is one candidate, not a conflict
            second = next((c for c in cands[1:] if c.entity.norm != best.entity.norm), None)
            partial_ok = best.entity.partial and best.distance == 0 and (
                catalog.df(words[0]) / catalog.doc_total < 0.05) and second is None
            if partial_ok:
                canon = best.entity.norm.split()
                return (tokens[:s] + canon + tokens[s + size:],
                        Correction(phrase, best.entity.canonical, best.entity.kind, "partial"))
            if not unknown and best.distance > 0:
                continue                                             # every word is real corpus vocabulary
            if best.distance == 0 and second is not None:             # ambiguous partial name
                return _clarify_from(phrase, [c for c in cands if c.distance == 0], tokens, s, s + size, ambiguous=True)
            clear = second is None or second.distance > best.distance
            if (clear and best.distance <= settings.autocorrect_max_edits and len(phrase) >= settings.autocorrect_min_length
                    and len(cands) >= 1 and best.distance >= 1):
                canon = best.entity.norm.split()
                return (tokens[:s] + canon + tokens[s + size:],
                        Correction(phrase, best.entity.canonical, best.entity.kind,
                                   "transliteration" if best.phonetic else "edit"))
            if residual_len > 3 and not cue_before:      # a suggestion needs a name-like query
                continue
            return _clarify_from(phrase, cands, tokens, s, s + size, clear_best=clear)
    return None


def _clarify_from(phrase: str, cands: list[Candidate], tokens: list[str], s: int, e: int, *,
                  ambiguous: bool = False, clear_best: bool = False) -> Clarification:
    cands = _unique(cands)[: settings.suggestion_max]
    names = list(dict.fromkeys(c.entity.canonical for c in cands))
    preview = None
    if cands and (clear_best or len(cands) == 1):
        preview = " ".join(tokens[:s] + cands[0].entity.norm.split() + tokens[e:]).replace(COMMA, ",")
    if ambiguous:
        msg = f"\"{phrase}\" matches more than one name. Which did you mean: " + _join_or(names) + "?"
        reason = "ambiguous_name"
    else:
        msg = f"I couldn't find \"{phrase}\". Did you mean " + _join_or(names) + "?"
        reason = "unknown_name"
    return Clarification(reason, msg, names, preview)


def _join_or(items: list[str]) -> str:
    items = [f"\"{x}\"" for x in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


# ---------------------------------------------------------------------------------------
# Finalisation: allowed asset set, constraint availability, notes
# ---------------------------------------------------------------------------------------
def _finalize(plan: Plan, catalog: Catalog) -> None:
    allowed: set[str] | None = None
    for f in plan.facets:
        if plan.mode == "similar" and f.kind == "title" and not f.negated:
            continue                                  # the title is the seed of the search, not a filter
        keys: set[str] = set()
        for e in f.alternatives:
            keys |= catalog.assets_for(e)
        if f.negated:
            base = set(catalog.assets) if allowed is None else allowed
            allowed = base - keys
        else:
            allowed = keys if allowed is None else (allowed & keys)
    cov = catalog.coverage()
    if plan.years is not None:
        if cov["original_release_year"] == 0:
            plan.notes.append(f"Release year ({plan.years.text}) cannot be applied: this catalog has no original "
                              "release dates. The filter was ignored.")
            plan.approximate = True
            plan.years = None
        else:
            base = set(catalog.assets) if allowed is None else allowed
            known = {k for k in base if catalog.assets[k].original_release_year is not None}
            allowed = {k for k in known if plan.years.contains(catalog.assets[k].original_release_year)}
            if len(known) < len(base):
                plan.notes.append(f"{len(base) - len(known)} title(s) without a release year were excluded "
                                  f"by the year filter ({plan.years.describe()}).")
    if plan.sort in ("latest", "oldest") and cov["original_release_date"] == 0 and cov["original_release_year"] == 0:
        plan.notes.append(f"\"{plan.sort}\" cannot be applied: this catalog has no original release dates. "
                          "Results are ranked by relevance.")
        plan.approximate = True
        plan.sort = None
    if plan.sort == "added" and cov["platform_release_date"] == 0:
        plan.sort = None
    plan.allowed = allowed
    if (plan.mode == "browse" and not plan.facets and plan.years is None and plan.sort is None
            and plan.limit is None):
        plan.mode = "clarify"
        plan.clarification = Clarification(
            "nothing_to_search", " ".join(plan.notes + ["Tell me a title, an actor, a genre, or describe a scene."]))
        return
    if allowed is not None and not allowed:
        plan.empty_reason = diagnose_empty(plan, catalog)
    if plan.mode == "similar":
        plan.similar_to = sorted(k for f in plan.facets if f.kind == "title" and not f.negated
                                 for e in f.alternatives for k in catalog.assets_for(e))
        if plan.allowed is not None:
            plan.allowed = plan.allowed - set(plan.similar_to)


def diagnose_empty(plan: Plan, catalog: Catalog) -> str:
    parts = []
    for f in plan.facets:
        keys: set[str] = set()
        for e in f.alternatives:
            keys |= catalog.assets_for(e)
        if f.negated:
            parts.append(f"excluding {', '.join(dict.fromkeys(e.canonical for e in f.alternatives))} leaves "
                         f"{len(set(catalog.assets) - keys)} title(s)")
        else:
            name = ", ".join(dict.fromkeys(e.canonical for e in f.alternatives))
            parts.append(f"{name}: {len(keys)} title(s)")
    if plan.years is not None:
        parts.append(f"year {plan.years.describe()}")
    return "No title satisfies all of the requested conditions (" + "; ".join(parts) + ")."


# ---------------------------------------------------------------------------------------
# Channels (which indexes to search, how, and with which weight)
# ---------------------------------------------------------------------------------------
def build_channels(plan: Plan, profile: str | None = None, methods: set[str] | None = None) -> list[ChannelSpec]:
    """Which indexes to search, how, and with which RRF weight (see the V2 plan, section on fusion).

    ``profile`` overrides settings.weight_profile ("catalog_preferred" | "equal"); ``methods`` restricts the
    channels to e.g. {"lex", "phrase"} (lexical only) or {"dense"} - used by the benchmark to compare
    lexical, semantic and hybrid retrieval on identical plans."""
    chans = _channels(plan, profile)
    return [c for c in chans if methods is None or c.method in methods]


def _channels(plan: Plan, profile: str | None) -> list[ChannelSpec]:
    wc, ws = settings.catalog_channel_weight, settings.scene_channel_weight
    if (profile or settings.weight_profile) == "equal":
        wc = ws = 1.0
    if plan.mode in ("title", "browse", "clarify"):
        return []
    if plan.mode == "similar":
        return [ChannelSpec("catalog_dense", "catalog", ("main",), "dense", 1.0)]
    has_text = bool(plan.terms or plan.dense_text or plan.phrase)
    if not has_text:
        return []
    if plan.mode == "quote":
        literal = bool(plan.phrase)
        out = []
        if literal:
            out.append(ChannelSpec("dialogue_phrase", "scene", ("dialogue",), "phrase", 1.0))
        out.append(ChannelSpec("dialogue_lex", "scene", ("dialogue",), "lex", 0.5 if literal else 1.0))
        if plan.terms:
            out.append(ChannelSpec("scene_lex", "scene", ("main",), "lex", 0.5 if literal else 1.0))
        out.append(ChannelSpec("scene_dense", "scene", ("main", "dialogue"), "dense", 0.5 if literal else 1.0))
        return out
    scene_only = plan.mode == "scene"
    wcat, wsc = (wc, ws) if plan.mode == "catalog" else (1.0, 1.0)        # mixed / uncertain -> equal
    scene = [ChannelSpec("scene_lex", "scene", ("main",), "lex", 1.0 if scene_only else wsc),
             ChannelSpec("dialogue_lex", "scene", ("dialogue",), "lex", 1.0 if scene_only else wsc),
             ChannelSpec("scene_dense", "scene", ("main", "dialogue"), "dense", 1.0 if scene_only else wsc)]
    if scene_only:
        return scene
    return [ChannelSpec("catalog_lex", "catalog", ("main",), "lex", wcat),
            ChannelSpec("catalog_dense", "catalog", ("main",), "dense", wcat)] + scene
