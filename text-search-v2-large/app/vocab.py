"""Single source for maintained English query vocabulary.

Genre synonyms, descriptive mood preferences, lexical alternatives, content-type
words, language aliases and grammar/operators are defined in this module.
Catalog titles, people and observed metadata labels still come from the database.
Edit this file and restart the app; no JSON synonym registry or loader is used.
"""

from __future__ import annotations

# Grammar for title/person intent; actual matching names always come from the catalog.
TITLE_REQUEST_CUES = {'play', 'watch', 'title', 'titled'}
EXPLICIT_PERSON_SLOTS = {'starring', 'featuring', 'actor', 'actress', 'character', 'director', 'directed', 'by', 'with'}
NARRATIVE_TITLE_BLOCKERS = {'about', 'where', 'whose', 'that', 'named'}


# --- genre synonyms (single definition source) --------------------------------------
GENRE_SYNONYMS = {'family': {'family', 'family oriented', 'family drama'},
 'comedy': {'comedies', 'humour', 'humor', 'humorous', 'comedic', 'funny', 'comedy', 'comic'},
 'romance': {'love story', 'rom', 'romance', 'romances', 'romantic'},
 'thriller': {'thrillers', 'suspenseful', 'suspense', 'thriller'},
 'horror': {'scary', 'horror', 'spooky', 'haunted', 'horrors'},
 'science fiction': {'sci fi', 'science fiction', 'scifi'},
 'documentary': {'documentary', 'docu', 'documentaries'},
 'animation': {'cartoon', 'animated', 'cartoons', 'animation'},
 'biography': {'biopic', 'biographical', 'biography', 'biopics'},
 'action': {'action packed', 'action', 'actions'},
 'mystery': {'mysteries', 'whodunnit', 'mystery', 'whodunit'},
 'adventure': {'adventure', 'adventures'},
 'crime': {'crime', 'crimes'},
 'drama': {'drama', 'dramas'},
 'fiction': {'fiction'}}

# --- descriptive preferences; these do not imply child suitability ------------------
MOOD_PREFERENCES = [{'name': 'family',
  'aliases': ['family', 'family oriented'],
  'hints': ['family', 'relationships', 'reconciliation'],
  'text': 'a story about family relationships and reconciliation'},
 {'name': 'feel good',
  'aliases': ['feel good', 'feelgood', 'heartwarming', 'uplifting', 'heart warming', 'comfort watch'],
  'hints': ['hope', 'happiness', 'joy'],
  'text': 'an uplifting heartwarming story of hope and happiness'},
 {'name': 'lighthearted',
  'aliases': ['lighthearted', 'light hearted', 'light watch', 'easy watch'],
  'hints': ['humor', 'comedy', 'joy'],
  'text': 'a lighthearted humorous story'},
 {'name': 'emotional',
  'aliases': ['emotional', 'tearjerker', 'tear jerker', 'moving story', 'touching story'],
  'hints': ['grief', 'loss', 'love'],
  'text': 'an emotional touching story of love and loss'},
 {'name': 'friendship',
  'aliases': ['friendship', 'friends bonding', 'bromance'],
  'hints': ['friendship', 'loyalty', 'companionship'],
  'text': 'a story about friendship loyalty and companionship'},
 {'name': 'revenge',
  'aliases': ['revenge', 'vengeance', 'getting even', 'get even'],
  'hints': ['revenge', 'vengeance', 'retribution'],
  'text': 'a story about revenge after betrayal'},
 {'name': 'inspirational',
  'aliases': ['inspirational', 'inspiring', 'motivational', 'underdog story'],
  'hints': ['perseverance', 'courage', 'triumph'],
  'text': 'an inspiring underdog story of perseverance and courage'}]

# --- lexical OR alternatives --------------------------------------------------------
LEXICAL_SYNONYM_GROUPS = [['kidnap', 'kidnapped', 'kidnapping', 'abduct', 'abducted', 'abduction'],
 ['revenge', 'vengeance', 'retribution'],
 ['heist', 'robbery', 'robberies'],
 ['betrayal', 'betrayed', 'treachery'],
 ['friendship', 'companionship'],
 ['heartwarming', 'uplifting', 'feelgood']]
LEXICAL_SYNONYMS = {word: group for group in LEXICAL_SYNONYM_GROUPS for word in group}

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
                                 "dubbing", "captions", "caption"}

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

# Audio availability is the only language concept in this catalog.
NOISE |= {"audio", "dubbed", "dubbing", "language", "languages", "version"}
LANGUAGE_NON_FILTER_FOLLOWERS -= {'dub','dubbed','dubbing','audio','version'}
