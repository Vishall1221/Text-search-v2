"""Maintained English vocabulary for query understanding.

Catalog names (titles, actors, characters, genres, languages) come from DATA, not from
this file. This file holds only the closed set of English *operators* and *aliases* the
plan keeps as explicit language rules: negation, date and sort words, content-type nouns,
scene/catalog scope markers and a few genre/language synonyms.

Vocabulary patterns here were adapted from the reference service (category word set,
explicit-language regex, "top N"/rank extraction, similar/recommend cue words).
"""

from __future__ import annotations

# --- function words / filler that never carry search meaning -------------------------
FUNCTION_WORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "from", "by",
    "is", "are", "was", "were", "be", "been", "being", "am", "me", "my", "i", "we", "us", "you",
    "your", "it", "its", "he", "she", "his", "her", "him", "they", "them", "their", "this", "that",
    "these", "those", "there", "here", "which", "what", "who", "whose", "whom", "where", "when",
    "how", "why", "do", "does", "did", "has", "have", "had", "as", "into", "onto", "up", "out",
    "some", "any", "all", "every", "each", "one", "ones", "also", "just", "only", "very", "so",
    "than", "then", "too", "if", "but", "nor", "s",
}
COMMAND_WORDS = {
    "find", "show", "play", "watch", "search", "get", "give", "list", "tell", "open", "start",
    "want", "need", "looking", "look", "wish", "like", "love", "please", "can", "could", "would",
    "should", "will", "let", "see", "suggest", "recommend", "recommendation", "recommendations",
    "something", "anything", "stuff", "about", "regarding", "related", "available", "stream",
    "streaming", "dikhao", "chalao", "lagao", "batao", "dikhana", "mujhe", "hai", "wali", "wala",
}
NOISE = FUNCTION_WORDS | COMMAND_WORDS

# Words that mark the *request type* and are not scene content.
SCENE_MARKERS = {
    "scene", "scenes", "moment", "moments", "clip", "clips", "sequence", "sequences", "shot",
    "shots", "dialogue", "dialogues", "dialog", "line", "lines", "quote", "quotes", "says",
    "said", "saying", "speaks", "shouts", "whispers", "timestamp",
}
DIALOGUE_MARKERS = {"dialogue", "dialogues", "dialog", "line", "lines", "quote", "quotes",
                    "says", "said", "saying", "speaks", "shouts", "whispers"}
CATALOG_MARKERS = {"plot", "storyline", "premise", "synopsis", "story"}
# "movie about", "film where", "story of", "set in" ... -> premise (catalog) requests.
CATALOG_PHRASES = [
    r"\b(?:movie|movies|film|films|show|shows|series|story)\s+(?:about|where|in which|that|of|set in|based on)\b",
    r"\bset in\b", r"\bbased on\b", r"\bis about\b",
]

# --- content type nouns -----------------------------------------------------------------
# 'show' is ambiguous: "show me ..." is a verb and must NOT become a TV-show filter.
CONTENT_TYPE_WORDS = {
    "movie": "movie", "movies": "movie", "film": "movie", "films": "movie", "cinema": "movie",
    "picture": "movie", "pictures": "movie", "feature": "movie",
    "series": "show", "serial": "show", "serials": "show", "webseries": "show", "shows": "show",
    "episode": "show", "episodes": "show", "tv": "show",
    "originals": "original",
}
# Content-type words that are also ordinary English ("serial murders", "a series of events", "psychotic
# episode"). They count as a content type only at the end of the query, before a connector / preposition,
# or directly before another filter word ("series tamil").
AMBIGUOUS_CONTENT_WORDS = {"serial", "serials", "series", "episode", "episodes", "tv", "feature", "picture",
                           "pictures", "cinema", "shows", "show"}
AMBIGUOUS_NEXT_OK = {"about", "where", "with", "in", "from", "set", "based", "starring", "featuring", "that",
                      "which", "by", "like", "not", "but", "without", "on", "for", "and", "or", "nor", "to",
                      "releasing", "released", "streaming", "available", "please"}
SHOW_NOUN_PRECEDERS = {"a", "the", "any", "some", "good", "best", "new", "latest", "popular",
                       "tv", "web", "reality", "comedy", "tamil", "hindi", "telugu", "bengali",
                       "marathi", "kannada", "malayalam", "punjabi"}
SHOW_VERB_FOLLOWERS = {"me", "us", "my", "all", "everything"}

# --- languages (query words -> canonical catalog value) ---------------------------------
LANGUAGE_ALIASES = {
    "bengali": "BENGALI", "bangla": "BENGALI",
    "hindi": "HINDI", "hindustani": "HINDI",
    "marathi": "MARATHI",
    "tamil": "TAMIL",
    "telugu": "TELUGU",
    "kannada": "KANNADA",
    "malayalam": "MALAYALAM",
    "punjabi": "PUNJABI", "panjabi": "PUNJABI",
    "gujarati": "GUJARATI",
    "odia": "ODIA", "oriya": "ODIA",
    "assamese": "ASSAMESE",
    "bhojpuri": "BHOJPURI",
    "urdu": "URDU",
    "english": "ENGLISH",
}
# "English subtitles", "Hindi dubbed" describe the audio/subtitle track, not the movie language.
LANGUAGE_NON_FILTER_FOLLOWERS = {"subtitle", "subtitles", "subtitled", "sub", "subs", "dub", "dubbed",
                                 "dubbing", "captions", "caption", "audio", "version"}

# --- genres: synonym groups (each word -> words that mean the same genre) -----------------
GENRE_SYNONYMS = {
    "romance": {"romance", "romantic", "romances", "rom"},
    "comedy": {"comedy", "comedies", "comic", "comedic"},
    "thriller": {"thriller", "thrillers", "suspense", "suspenseful"},
    "horror": {"horror", "horrors", "scary", "spooky", "haunted"},
    "action": {"action", "actions"},
    "drama": {"drama", "dramas"},
    "mystery": {"mystery", "mysteries", "whodunit"},
    "crime": {"crime", "crimes"},
    "adventure": {"adventure", "adventures"},
    "science fiction": {"scifi", "sci fi", "science fiction"},
    "fiction": {"fiction"},
}
# Genre word directly followed by one of these describes a SCENE ("action scene"), not a genre.
GENRE_SCENE_FOLLOWERS = SCENE_MARKERS

# --- negation / exclusion --------------------------------------------------------------
NEGATION_CUES = [
    ("but", "not"), ("other", "than"), ("apart", "from"), ("anything", "but"),
    ("not",), ("no",), ("without",), ("except",), ("excluding",), ("exclude",), ("minus",), ("nor",),
]
CONNECTORS = {"or", "and", "nor", "&", ","}

# --- cues that introduce an entity of a given kind ------------------------------------------
ACTOR_CUES = {"starring", "stars", "featuring", "features", "actor", "actress", "cast", "casting",
              "starred", "with"}
DIRECTOR_CUES = {"director", "directed", "directors"}
BY_CUES = {"by"}

# --- sorting / limits / popularity ------------------------------------------------------------
SORT_LATEST = [("most", "recent"), ("newly", "released"), ("latest",), ("newest",), ("recent",),
               ("recently", "released"), ("new",)]
SORT_OLDEST = [("oldest",), ("earliest",), ("old", "classic"), ("classic",)]
POPULARITY = [("most", "watched"), ("most", "viewed"), ("most", "popular"), ("highest", "rated"),
              ("top", "rated"), ("best", "rated"), ("box", "office"), ("popular",), ("trending",),
              ("blockbuster",), ("blockbusters",), ("viral",), ("leaderboard",)]
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20}
LIMIT_CUES = {"top", "first", "any", "only", "just", "best"}
LIMIT_NOUNS = {"results", "result", "titles", "title", "options", "option", "suggestions",
               "suggestion", "picks", "recommendations"}

# --- date words -------------------------------------------------------------------------------
DECADE_WORDS = {"twenties": 1920, "thirties": 1930, "forties": 1940, "fifties": 1950, "sixties": 1960,
                "seventies": 1970, "eighties": 1980, "nineties": 1990, "noughties": 2000,
                "aughts": 2000}
BEFORE_CUES = ["before", "until", "till", "prior to", "earlier than", "older than", "up to", "pre"]
AFTER_CUES = ["after", "later than", "newer than", "post"]
SINCE_CUES = ["since", "from", "starting", "onwards", "onward"]
IN_YEAR_CUES = ["in", "during", "released in", "of", "from the year", "year"]

# --- "similar to" (standalone, catalog-embedding based; no conversation state) ---------------
SIMILAR_CUES = [("similar", "to"), ("similar", "like"), ("more", "like"), ("movies", "like"),
                ("films", "like"), ("shows", "like"), ("just", "like"), ("like",)]
