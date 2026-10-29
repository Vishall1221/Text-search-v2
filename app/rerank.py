"""Optional cross-encoder reranker (off by default; enable only after it passes the release gate).

The reranker rescoring is applied to the top ``rerank_depth`` fused assets. It runs after fusion, so it
can only reorder candidates the first stage already found - it never adds recall. Latency and quality
are benchmarked at depths 20 / 30 / 50 with separate evaluation tooling before this is switched on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import numpy as np

log = logging.getLogger("rerank")


class CrossEncoder:
    def __init__(self, model_dir: Path, threads: int = 2, max_len: int = 256) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        onnx = next((p for p in (model_dir / "onnx" / "model.onnx", model_dir / "model.onnx") if p.exists()), None)
        tok = model_dir / "tokenizer.json"
        if onnx is None or not tok.exists():
            raise FileNotFoundError(f"Reranker files not found in {model_dir}")
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self._session = ort.InferenceSession(str(onnx), so, providers=["CPUExecutionProvider"])
        self._names = {i.name for i in self._session.get_inputs()}
        self._tok = Tokenizer.from_file(str(tok))
        self._tok.enable_truncation(max_length=max_len)
        self._tok.enable_padding()

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        enc = self._tok.encode_batch([(query, p) for p in passages])
        ids = np.asarray([e.ids for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": np.asarray([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in self._names:
            feed["token_type_ids"] = np.asarray([e.type_ids for e in enc], dtype=np.int64)
        logits = self._session.run(None, feed)[0]
        return [float(x) for x in np.asarray(logits).reshape(len(passages), -1)[:, 0]]


def load_reranker(model_dir: Path, threads: int = 2) -> CrossEncoder | None:
    try:
        return CrossEncoder(model_dir, threads)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Reranker disabled: %s", exc)
        return None
