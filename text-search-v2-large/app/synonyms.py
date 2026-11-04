"""Editable vocabulary loaded at process startup, never learned from user queries."""
import json
from pathlib import Path

REGISTRY = json.loads((Path(__file__).resolve().parents[1]/'config/query_synonyms.json').read_text(encoding='utf-8'))
LEXICAL = {word: group for group in REGISTRY['lexical'] for word in group}
