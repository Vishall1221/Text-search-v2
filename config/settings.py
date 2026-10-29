"""Single source of runtime configuration for Text Search V2.

Values come from environment variables first, then ``config/.env``.
Externally calibrated thresholds are read
from ``config/calibration.json`` when present.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).resolve().parent
ROOT_DIR = CONFIG_DIR.parent
load_dotenv(CONFIG_DIR / ".env", override=False)


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().casefold() in {"1", "true", "yes", "on"}


def _float_or_none(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else None


def _calibration() -> dict:
    path = Path(os.getenv("CALIBRATION_FILE", str(CONFIG_DIR / "calibration.json")))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_CAL = _calibration()
# A threshold belongs to one embedding model: ignore it if the configured model is a different one.
if _CAL and _CAL.get("model") and _CAL["model"].casefold() != os.getenv("EMBED_MODEL", "bge-small-en-v1.5").casefold():
    _CAL = {}


@dataclass(frozen=True)
class Settings:
    calibration: dict = field(default_factory=lambda: dict(_CAL))
    dense_threshold_explicit: bool = bool(os.getenv("DENSE_MIN_SIMILARITY", "").strip())
    # --- service -----------------------------------------------------------
    database_url: str = os.getenv("DATABASE_URL", "postgresql://ai_search@127.0.0.1:5432/ai_search")
    api_host: str = os.getenv("API_HOST", "127.0.0.1")
    api_port: int = int(os.getenv("API_PORT", "8001"))
    db_pool_min: int = int(os.getenv("DB_POOL_MIN", "2"))
    db_pool_max: int = int(os.getenv("DB_POOL_MAX", "8"))
    request_deadline_ms: int = int(os.getenv("REQUEST_DEADLINE_MS", "950"))
    catalog_cache_seconds: float = float(os.getenv("CATALOG_CACHE_SECONDS", "30"))
    generation_check_seconds: float = float(os.getenv("GENERATION_CHECK_SECONDS", "2"))
    debug_diagnostics: bool = _bool("DEBUG_DIAGNOSTICS", True)

    # --- embeddings --------------------------------------------------------
    embed_model: str = os.getenv("EMBED_MODEL", "bge-small-en-v1.5")
    models_dir: Path = Path(os.getenv("MODELS_DIR", str(ROOT_DIR / "models")))
    embed_threads: int = int(os.getenv("EMBED_THREADS", "2"))
    embed_concurrency: int = int(os.getenv("EMBED_CONCURRENCY", "2"))
    dense_enabled: bool = _bool("DENSE_ENABLED", True)
    # Raw cosine is not a probability. A threshold is only used once it has been
    # calibrated on independently labelled data for this catalog.
    dense_min_similarity: float | None = (
        _float_or_none("DENSE_MIN_SIMILARITY")
        if os.getenv("DENSE_MIN_SIMILARITY", "").strip()
        else _CAL.get("dense_min_similarity")
    )

    # --- document construction ---------------------------------------------
    text_version: str = os.getenv("TEXT_VERSION", "tv2-2")
    passage_tokens: int = int(os.getenv("PASSAGE_TOKENS", "384"))
    passage_overlap_sentences: int = int(os.getenv("PASSAGE_OVERLAP_SENTENCES", "0"))
    scene_context_header: bool = _bool("SCENE_CONTEXT_HEADER", False)

    # --- lexical retrieval -------------------------------------------------
    lex_rank_fn: str = os.getenv("LEX_RANK_FN", "ts_rank_cd")      # ts_rank_cd | ts_rank
    lex_rank_norm: int = int(os.getenv("LEX_RANK_NORM", "1"))
    lex_relax: bool = _bool("LEX_RELAX", True)
    lex_min_candidates: int = int(os.getenv("LEX_MIN_CANDIDATES", "5"))
    # Relaxed (all-but-one / OR) stages keep only documents matching at least this share of the query terms
    # (queries of <= 2 terms: any). Stops one common word from "matching" an out-of-catalog request.
    lex_min_match_ratio: float = float(os.getenv("LEX_MIN_MATCH_RATIO", "0.5"))

    # --- fusion ------------------------------------------------------------
    candidate_window: int = int(os.getenv("CANDIDATE_WINDOW", "50"))
    # Nearest documents considered before grouping by title (dense). Larger = more title diversity, slower.
    dense_candidates: int = int(os.getenv("DENSE_CANDIDATES", "1000"))
    rrf_k: int = int(os.getenv("RRF_K", "60"))
    weight_profile: str = os.getenv("WEIGHT_PROFILE", "catalog_preferred")   # or "equal"
    catalog_channel_weight: float = float(os.getenv("CATALOG_CHANNEL_WEIGHT", "1.0"))
    scene_channel_weight: float = float(os.getenv("SCENE_CHANNEL_WEIGHT", "0.5"))
    multi_scene_bonus: float = float(os.getenv("MULTI_SCENE_BONUS", "0.0"))   # disabled initially
    multi_scene_bonus_scenes: int = int(os.getenv("MULTI_SCENE_BONUS_SCENES", "3"))
    scenes_per_card: int = int(os.getenv("SCENES_PER_CARD", "5"))

    # --- reranking (disabled by default; enable only after benchmarking) ----
    rerank_enabled: bool = _bool("RERANK_ENABLED", False)
    rerank_depth: int = int(os.getenv("RERANK_DEPTH", "20"))
    rerank_model_dir: Path = Path(os.getenv("RERANK_MODEL_DIR", str(ROOT_DIR / "models" / "ms-marco-MiniLM-L-6-v2")))

    # --- spelling / clarification -----------------------------------------
    autocorrect_max_edits: int = int(os.getenv("AUTOCORRECT_MAX_EDITS", "1"))
    autocorrect_min_length: int = int(os.getenv("AUTOCORRECT_MIN_LENGTH", "5"))
    suggestion_max: int = int(os.getenv("SUGGESTION_MAX", "3"))
    preview_results: int = int(os.getenv("PREVIEW_RESULTS", "3"))

    # --- dialogue ----------------------------------------------------------
    dialogue_across_turns: bool = _bool("DIALOGUE_ACROSS_TURNS", True)

    # --- data profile ------------------------------------------------------
    import_profile: Path = Path(os.getenv("IMPORT_PROFILE", str(CONFIG_DIR / "import_profile.json")))
    aliases_file: Path = Path(os.getenv("ALIASES_FILE", str(CONFIG_DIR / "aliases.json")))


settings = Settings()
