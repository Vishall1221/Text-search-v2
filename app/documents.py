"""Derived search documents built on natural boundaries.

* One catalog document per asset (title, synopsis, genres). Cast stays a structured filter.
* One scene "main" document per scene (description, summaries, setting, events, objects,
  characters, mood, tags) and one scene "dialogue" document (speaker turns).
* Split ONLY records that exceed the passage limit, at sentence boundaries; overlap is
  configurable and defaults to zero.
* The reference data repeats the movie title and language in every scene summary and tag
  ("Film: X (LANG) | Act: .. | Scene: .. |" and tags such as "neel-shimana"); that template
  is stripped so a title can never make every scene look relevant.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app.text import fold, normalize

_TEMPLATE_PREFIX = re.compile(r"^\s*Film:\s*.*?\|\s*Act:\s*.*?\|\s*Scene:\s*\S+\s*\|\s*", re.IGNORECASE | re.DOTALL)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'\[(])")
_TURN_START = re.compile(
    r"(?:^[ \t]*|(?<=\))[ \t]+)(?:\[(?P<b>[^\]\r\n]{1,60})\]|(?P<p>[A-Z][A-Za-z0-9.'’ ]{0,40})):[ \t]*",
    re.MULTILINE)
_GLOSS = re.compile(r"\(([^()]*)\)\s*$")


# ---------------------------------------------------------------------------------------
# Dialogue
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Turn:
    speaker: str | None
    original: str
    gloss: str

    def as_dict(self) -> dict[str, Any]:
        return {"speaker": self.speaker, "original": self.original, "gloss": self.gloss}


def parse_dialogue(transcript: str | None) -> list[Turn]:
    """Split ``[Speaker]: native text (English gloss)`` and ``Speaker: native (gloss)`` into turns."""
    text = (transcript or "").strip()
    if not text:
        return []
    starts = list(_TURN_START.finditer(text))
    if not starts:
        return [_make_turn(None, text)]
    turns: list[Turn] = []
    if text[:starts[0].start()].strip():
        turns.append(_make_turn(None, text[:starts[0].start()].strip()))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        speaker = (m.group("b") or m.group("p") or "").strip() or None
        body = text[m.end():end].strip()
        if body:
            turns.append(_make_turn(speaker, body))
    return turns or [_make_turn(None, text)]


def _make_turn(speaker: str | None, body: str) -> Turn:
    m = _GLOSS.search(body)
    if m:
        return Turn(speaker, body[:m.start()].strip(), m.group(1).strip())
    return Turn(speaker, body.strip(), "")


_NON_DIALOGUE = {"silence", "no dialogue", "none", "n a", "na", "music", "instrumental", "background music",
                 "no speech", "silent"}


def is_placeholder_turn(t: Turn) -> bool:
    """'Silence.' and similar markers are not speech and must not become searchable lines."""
    return normalize(t.gloss or t.original) in _NON_DIALOGUE and not t.speaker


def dialogue_embed_text(turns: Iterable[Turn]) -> str:
    """English text for embedding: the gloss (or the line itself when there is none)."""
    parts = []
    for t in turns:
        line = t.gloss or t.original
        parts.append(f"{t.speaker}: {line}" if t.speaker else line)
    return " ".join(parts)


def dialogue_lexical_text(turns: Iterable[Turn]) -> str:
    """Index text for the 'simple' configuration: lines only (speaker labels stripped)."""
    return fold(" ¶ ".join(" ".join(x for x in (t.original, t.gloss) if x) for t in turns))


# ---------------------------------------------------------------------------------------
# Scene text
# ---------------------------------------------------------------------------------------

def clean_master_summary(summary: str | None, description: str | None) -> str:
    """Drop the templated prefix; drop the remainder when it only truncates the description."""
    s = _TEMPLATE_PREFIX.sub("", summary or "").strip()
    if not s:
        return ""
    core = normalize(s.rstrip(". ")).rstrip(" ")
    core = core[:-3].strip() if s.endswith("...") else core
    if core and normalize(description or "").startswith(core[: max(10, len(core) - 3)]):
        return ""
    if s.endswith("..."):
        return ""
    return s


def _listify(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            out.extend(_listify(v))
        return out
    return []


def event_labels(scene: dict[str, Any]) -> list[str]:
    """Events appear under different keys across sources: tag / event / event_type / ..."""
    labels: list[str] = []
    for ev in scene.get("events") or []:
        if isinstance(ev, dict):
            for key in ("tag", "event", "event_type", "type", "label", "name"):
                if isinstance(ev.get(key), str) and ev[key].strip():
                    labels.append(ev[key].strip())
                    break
            if isinstance(ev.get("description"), str) and ev["description"].strip():
                labels.append(ev["description"].strip())
        elif isinstance(ev, str) and ev.strip():
            labels.append(ev.strip())
    for km in scene.get("key_moments") or []:
        if isinstance(km, dict) and isinstance(km.get("label"), str):
            labels.append(km["label"].strip())
    return list(dict.fromkeys(labels))


def useful_tags(scene: dict[str, Any], title: str, language: str) -> list[str]:
    """Tags minus the movie-level template (title slug, language) and duplicates of other fields."""
    banned = {normalize(title), normalize(language), normalize(f"{title} {language}")}
    mood = normalize(scene.get("mood") or "")
    chars = {normalize(c) for c in _listify(scene.get("entities_characters"))}
    out: list[str] = []
    for tag in _listify(scene.get("tags")):
        n = normalize(tag.replace("-", " "))
        if not n or n in banned or n == mood or n in chars:
            continue
        out.append(tag.replace("-", " ").strip())
    return list(dict.fromkeys(out))


def scene_fields(scene: dict[str, Any], title: str, language: str) -> dict[str, Any]:
    """Resolved scene facts used for both the lexical weights and the embedding text."""
    visual = scene.get("visual") if isinstance(scene.get("visual"), dict) else {}
    desc = (scene.get("description") or "").strip()
    return {
        "description": desc,
        "summary": clean_master_summary(scene.get("master_scene_summary"), desc),
        "visual_summary": (scene.get("visual_summary") or "").strip(),
        "setting": (visual.get("setting") or "").strip(),
        "themes": _listify(visual.get("actions/Theme")),
        "events": event_labels(scene),
        "objects": _listify(scene.get("entities_objects")),
        "characters": _listify(scene.get("entities_characters")),
        "mood": (scene.get("mood") or "").strip(),
        "beat": (scene.get("narrative_beat") or {}).get("label", "") if isinstance(scene.get("narrative_beat"), dict) else "",
        "tags": useful_tags(scene, title, language),
    }


def scene_sentences(f: dict[str, Any]) -> list[str]:
    """Natural-language units, each atomic for splitting."""
    sents: list[str] = []
    for key in ("description", "summary", "visual_summary"):
        if f[key]:
            sents.extend(s for s in _SENTENCE_SPLIT.split(f[key]) if s.strip())
    if f["setting"]:
        sents.append(f"Setting: {f['setting']}.")
    if f["events"]:
        sents.append("Events: " + "; ".join(f["events"]) + ".")
    if f["objects"]:
        sents.append("Objects: " + ", ".join(f["objects"]) + ".")
    if f["characters"]:
        sents.append("Characters: " + ", ".join(f["characters"]) + ".")
    mood = ", ".join(x for x in (f["mood"], f["beat"]) if x)
    if mood:
        sents.append(f"Mood: {mood}.")
    if f["tags"]:
        sents.append("Tags: " + ", ".join(f["tags"]) + ".")
    return sents


def scene_lexical_weights(f: dict[str, Any]) -> dict[str, str]:
    """A..D text for the weighted 'english' vector (A most important)."""
    return {
        "A": fold(" ".join(x for x in (f["description"], f["setting"]) if x)),
        "B": fold(" ".join(f["events"] + f["objects"] + f["characters"])),
        "C": fold(" ".join(f["tags"] + [f["mood"], f["beat"]] + f["themes"])),
        "D": fold(" ".join(x for x in (f["summary"], f["visual_summary"]) if x)),
    }


# ---------------------------------------------------------------------------------------
# Passage splitting
# ---------------------------------------------------------------------------------------

def _split_to_fit(text: str, count: Callable[[str], int], max_tokens: int) -> list[str]:
    """Split only oversized text; test prospective output using the real tokenizer.

    A pathological single word may itself exceed the model budget. In that case
    split on character boundaries instead of silently dropping its tail.
    ``count`` may include a fixed header, speaker and special-token overhead.
    """
    if max_tokens < 1:
        raise ValueError("Passage token limit must be positive")
    if not text.strip():
        return []
    if count(text) <= max_tokens:
        return [text]
    out: list[str] = []
    cur = ""
    for word in text.split():
        candidate = f"{cur} {word}" if cur else word
        if count(candidate) <= max_tokens:
            cur = candidate
            continue
        if cur:
            out.append(cur)
            cur = ""
        if count(word) <= max_tokens:
            cur = word
            continue
        fragment = ""
        for char in word:
            if count(fragment + char) <= max_tokens:
                fragment += char
            else:
                if not fragment or count(char) > max_tokens:
                    raise ValueError("Passage budget cannot fit the header/speaker and one text character")
                out.append(fragment)
                fragment = char
        cur = fragment
    if cur:
        out.append(cur)
    return out


def split_passages(sentences: list[str], count: Callable[[str], int], max_tokens: int,
                   overlap: int = 0) -> list[list[str]]:
    """Pack natural boundaries while counting the exact joined output, including overlap.

    ``count`` must count the final embedded input, including special tokens and any
    fixed prefix. Sentence counts are not additive for subword tokenizers.
    """
    units = [part for s in sentences for part in _split_to_fit(s, count, max_tokens)]
    passages: list[list[str]] = []
    cur: list[str] = []
    for unit in units:
        if cur and count(" ".join([*cur, unit])) > max_tokens:
            passages.append(cur)
            keep = list(cur[-overlap:]) if overlap > 0 else []
            while keep and count(" ".join([*keep, unit])) > max_tokens:
                keep.pop(0)
            cur = keep
        cur.append(unit)
    if cur:
        passages.append(cur)
    return passages


# ---------------------------------------------------------------------------------------
# Passage objects
# ---------------------------------------------------------------------------------------

@dataclass
class Passage:
    scope: str                     # catalog | scene
    kind: str                      # main | dialogue
    asset_id: str
    language: str
    scene_id: str | None
    passage_no: int
    text: str                      # exact text that is embedded
    token_count: int
    lex_weights: dict[str, str] = field(default_factory=dict)   # A..D for tsv_main
    lex_simple: str | None = None                               # for tsv_simple

    @property
    def asset_key(self) -> str:
        return f"{self.asset_id}|{self.language}"


def text_hash(model_id: str, text_version: str, p: Passage) -> str:
    raw = f"{model_id}|{text_version}|{p.scope}|{p.kind}|{p.text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_catalog_passages(asset: dict[str, Any], count: Callable[[str], int], max_tokens: int,
                           overlap: int) -> list[Passage]:
    title, desc = asset["title"], (asset.get("description") or "").strip()
    genres = asset.get("genres") or []
    sents = [f"{title}."]
    if genres:
        sents.append("Genres: " + ", ".join(genres) + ".")
    sents.extend(s for s in _SENTENCE_SPLIT.split(desc) if s.strip())
    out: list[Passage] = []
    for n, group in enumerate(split_passages(sents, count, max_tokens, overlap), start=1):
        text = " ".join(group)
        weights = {"A": fold(title) if n == 1 else "", "B": fold(" ".join(genres)) if n == 1 else "",
                   "C": fold(" ".join(g for g in group if not g.startswith(title + ".") and not g.startswith("Genres:"))),
                   "D": ""}
        out.append(Passage("catalog", "main", asset["asset_id"], asset["language"], None, n, text,
                           count(text), lex_weights=weights))
    return out


def build_scene_passages(asset: dict[str, Any], scene: dict[str, Any], count: Callable[[str], int],
                         max_tokens: int, overlap: int, header: bool) -> tuple[list[Passage], list[Turn]]:
    f = scene_fields(scene, asset["title"], asset["language"])
    sents = scene_sentences(f)
    head = f"{asset['title']} ({', '.join(asset.get('genres') or [])}). " if header else ""
    out: list[Passage] = []
    final_count = lambda body: count(head + body)
    groups = split_passages(sents, final_count, max_tokens, overlap)
    weights = scene_lexical_weights(f)
    for n, group in enumerate(groups, start=1):
        text = head + " ".join(group)
        # Lexical weights are computed from the whole scene once and attached to passage 1;
        # later passages carry their own sentences as weight C so no sentence is unsearchable.
        lw = weights if n == 1 else {"A": "", "B": "", "C": fold(" ".join(group)), "D": ""}
        out.append(Passage("scene", "main", asset["asset_id"], asset["language"], scene["scene_id"], n,
                           text, count(text), lex_weights=lw))
    turns = parse_dialogue(scene.get("dialogue_transcript") or scene.get("important_dialogue"))
    speech = [t for t in turns if not is_placeholder_turn(t)]
    for n, group in enumerate(group_turns(speech, count, max_tokens, prefix=head), start=1):
        text = head + " ".join(dialogue_embed_text([t]) for t in group)
        out.append(Passage("scene", "dialogue", asset["asset_id"], asset["language"], scene["scene_id"], n,
                           text, count(text), lex_simple=dialogue_lexical_text(group)))
    return out, turns


def group_turns(turns: list[Turn], count: Callable[[str], int], max_tokens: int,
                prefix: str = "") -> list[list[Turn]]:
    """Pack whole turns into passages. A turn is never split across passages unless that single
    turn alone exceeds the limit, in which case it is cut at word boundaries (every fragment keeps
    the speaker and the same original/gloss text so literal search still finds it)."""
    groups: list[list[Turn]] = []
    cur: list[Turn] = []
    for t in turns:
        pieces = [t]
        if count(prefix + dialogue_embed_text([t])) > max_tokens:
            label = f"{t.speaker}: " if t.speaker else ""
            fragments = _split_to_fit(t.gloss or t.original,
                                     lambda line: count(prefix + label + line), max_tokens)
            pieces = [Turn(t.speaker, t.original if i == 0 else "", line)
                      if t.gloss else Turn(t.speaker, line, "") for i, line in enumerate(fragments)]
        for piece in pieces:
            if cur and count(prefix + dialogue_embed_text([*cur, piece])) > max_tokens:
                groups.append(cur)
                cur = []
            cur.append(piece)
    if cur:
        groups.append(cur)
    return groups
