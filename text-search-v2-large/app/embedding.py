"""Embedding backends.

``OnnxEmbedder``  - resident ONNX Runtime CPU session + HuggingFace ``tokenizers``; used for
                    BGE-small (default), Snowflake Arctic-embed-s, Granite-small-r2 and MiniLM.
``HashEmbedder``  - deterministic feature-hashing vectors, **tests only** (no semantics).

All profiles produce 384-dimensional L2-normalised vectors, so the ``vector(384)`` column and
the inner-product operator work for every candidate and a model swap only replaces vectors.
The same profile (model file, pooling, prefix, max length) is recorded on every index
generation and must match at query time; otherwise dense retrieval is disabled (stale-index
prevention).
"""

from __future__ import annotations

import hashlib
import math
import re
import zlib
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from config.settings import settings

DIM = 384
BGE_PREFIX = "Represent this sentence for searching relevant passages: "
_ACTIVE_TOKENIZER: ContextVar[tuple[object, object] | None] = ContextVar("embedding_tokenizer", default=None)


@dataclass(frozen=True)
class ModelProfile:
    name: str
    repo: str                 # HuggingFace repo used by scripts/download_model.py
    onnx_file: str
    pooling: str              # "cls" | "mean"
    query_prefix: str
    max_len: int              # model input limit
    dim: int = DIM
    verified: bool = True     # False -> check pooling/prefix on the model card before use
    note: str = ""


PROFILES: dict[str, ModelProfile] = {
    "bge-small-en-v1.5": ModelProfile(
        "bge-small-en-v1.5", "BAAI/bge-small-en-v1.5", "onnx/model.onnx", "cls", BGE_PREFIX, 512,
        note="Initial model of the V2 plan. Prefix is applied to queries only (recommended for "
             "short query vs long passage)."),
    "snowflake-arctic-embed-s": ModelProfile(
        "snowflake-arctic-embed-s", "Snowflake/snowflake-arctic-embed-s", "onnx/model.onnx", "cls",
        BGE_PREFIX, 512, note="Near drop-in challenger: 33M params, 384 d, Apache-2.0."),
    "granite-embedding-small-english-r2": ModelProfile(
        "granite-embedding-small-english-r2", "ibm-granite/granite-embedding-small-english-r2",
        "onnx/model.onnx", "cls", "", 8192, verified=False,
        note="Challenger with an 8192-token context. UNVERIFIED defaults: confirm pooling, prefix "
             "and the ONNX export on the model card before benchmarking."),
    "all-minilm-l6-v2": ModelProfile(
        "all-minilm-l6-v2", "sentence-transformers/all-MiniLM-L6-v2", "onnx/model.onnx", "mean", "", 256,
        note="Small general model; used for local tests because it is already on the dev PC."),
    "hash-test": ModelProfile("hash-test", "", "", "none", "", 10_000,
                              note="Feature hashing. TESTS ONLY - has no semantic knowledge."),
}


class Embedder(Protocol):
    profile: ModelProfile
    revision: str

    def encode_docs(self, texts: Sequence[str]) -> np.ndarray: ...
    def encode_query(self, text: str) -> np.ndarray: ...
    def count_tokens(self, text: str) -> int: ...


def approx_tokens(text: str) -> int:
    return max(1, math.ceil(len(text.split()) * 1.35))


class HashEmbedder:
    revision = "hash-v1"

    def __init__(self) -> None:
        self.profile = PROFILES["hash-test"]

    @staticmethod
    def _stem(w: str) -> str:
        for suf in ("ing", "es", "ed", "s"):
            if len(w) > len(suf) + 3 and w.endswith(suf):
                return w[: -len(suf)]
        return w

    def _vec(self, text: str) -> np.ndarray:
        toks = [self._stem(t) for t in re.findall(r"[^\W_]+", text.casefold())]
        v = np.zeros(DIM, dtype=np.float32)
        feats = [(t, 1.0) for t in toks] + [(f"{a}_{b}", 0.5) for a, b in zip(toks, toks[1:])]
        for f, w in feats:
            h = zlib.crc32(f.encode("utf-8"))
            v[h % DIM] += w * (1.0 if (h >> 16) & 1 else -1.0)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def encode_docs(self, texts: Sequence[str]) -> np.ndarray:
        return np.vstack([self._vec(t) for t in texts]) if texts else np.zeros((0, DIM), np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        return self._vec(text)

    def count_tokens(self, text: str) -> int:
        return approx_tokens(text)


class OnnxEmbedder:
    @property
    def _tok(self):
        """The current call's immutable tokenizer; never reconfigure a shared instance."""
        active = _ACTIVE_TOKENIZER.get()
        return active[1] if active is not None and active[0] is self else self._base_tok

    @_tok.setter
    def _tok(self, value):
        self._base_tok = value

    def __init__(self, profile: ModelProfile, model_dir: Path, threads: int = 2) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.profile = profile
        onnx_path = model_dir / profile.onnx_file
        if not onnx_path.exists():           # also accept a flat layout: <dir>/model.onnx
            onnx_path = model_dir / Path(profile.onnx_file).name
        tok_path = model_dir / "tokenizer.json"
        if not onnx_path.exists() or not tok_path.exists():
            raise FileNotFoundError(
                f"Model files for '{profile.name}' not found in {model_dir}. "
                f"Run: python -m scripts.download_model {profile.name}")
        h = hashlib.sha256()
        with onnx_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
        self.revision = h.hexdigest()[:16]

        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(onnx_path), so, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        max_len = min(profile.max_len, 512)
        self._tok = Tokenizer.from_file(str(tok_path))
        self._tok.enable_truncation(max_length=max_len)
        self._tok.enable_padding()
        self._count_tok = Tokenizer.from_file(str(tok_path))     # no truncation: for token counting
        self._count_tok.no_truncation()
        self._count_tok.no_padding()
        self._query_max = 64
        self._query_tok = Tokenizer.from_file(str(tok_path))
        self._query_tok.enable_truncation(max_length=min(max_len, self._query_max + len(profile.query_prefix.split()) * 2))
        self._query_tok.enable_padding()

    # -- encoding -------------------------------------------------------------------------
    def _run(self, texts: Sequence[str]) -> np.ndarray:
        enc = self._tok.encode_batch(list(texts))
        ids = np.asarray([e.ids for e in enc], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feed)[0]                     # (batch, seq, dim)
        return self.pool(hidden, mask, self.profile.pooling)

    @staticmethod
    def pool(hidden: np.ndarray, mask: np.ndarray, pooling: str) -> np.ndarray:
        if pooling == "cls":
            out = hidden[:, 0, :]
        else:
            m = mask[:, :, None].astype(np.float32)
            out = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        norm = np.linalg.norm(out, axis=1, keepdims=True)
        return (out / np.clip(norm, 1e-12, None)).astype(np.float32)

    def encode_docs(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), np.float32)
        parts = [self._run(texts[i:i + 32]) for i in range(0, len(texts), 32)]
        return np.vstack(parts)

    def encode_query(self, text: str) -> np.ndarray:
        text = self.profile.query_prefix + text
        # Query and document calls may overlap in the executor. Context-local selection
        # avoids changing either tokenizer's truncation settings while another call encodes.
        tok = getattr(self, "_query_tok", None)
        if tok is None:  # permits lightweight test/custom instances created without model loading
            from tokenizers import Tokenizer
            tok = Tokenizer.from_str(self._base_tok.to_str())
            tok.enable_truncation(max_length=min(self.profile.max_len, self._query_max + len(self.profile.query_prefix.split()) * 2))
        token = _ACTIVE_TOKENIZER.set((self, tok))
        try:
            return self._run([text])[0]
        finally:
            _ACTIVE_TOKENIZER.reset(token)

    def count_tokens(self, text: str) -> int:
        return len(self._count_tok.encode(text, add_special_tokens=True).ids)


def load_embedder(name: str | None = None, *, allow_hash: bool = False) -> Embedder:
    name = (name or settings.embed_model).casefold()
    if name == "hash-test":
        if not allow_hash:
            raise RuntimeError("'hash-test' is a test-only embedder; pass allow_hash=True explicitly")
        return HashEmbedder()
    if name not in PROFILES:
        raise KeyError(f"Unknown embedding model '{name}'. Known: {', '.join(PROFILES)}")
    return OnnxEmbedder(PROFILES[name], settings.models_dir / name, threads=settings.embed_threads)


def vector_literal(vec: np.ndarray) -> str:
    """pgvector text form, e.g. '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"
