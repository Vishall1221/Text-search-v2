"""Editable vocabulary loaded at process startup, never learned from user queries."""
import json
from pathlib import Path

REGISTRY = json.loads((Path(__file__).resolve().parents[1]/'config/query_synonyms.json').read_text(encoding='utf-8'))
GENRE_SYNONYMS = {name: set(values) for name, values in REGISTRY['genres'].items()}
LEXICAL = {word: group for group in REGISTRY['lexical'] for word in group}
