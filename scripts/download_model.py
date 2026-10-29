"""Download an embedding model (ONNX + tokenizer) into models/<name>/.

    python -m scripts.download_model bge-small-en-v1.5
    python -m scripts.download_model --list

Requires internet access to huggingface.co once; afterwards the service runs offline.
If the repository does not ship an ONNX file, export one with ``optimum``:
    pip install optimum[onnxruntime]
    optimum-cli export onnx --model BAAI/bge-small-en-v1.5 models/bge-small-en-v1.5/
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

from app.embedding import PROFILES
from config.settings import settings

HF = "https://huggingface.co/{repo}/resolve/main/{path}"


def fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  {url}\n    -> {dest}")
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as out:   # noqa: S310 (https only)
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    tmp.replace(dest)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?", default=settings.embed_model)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for p in PROFILES.values():
            flag = "" if p.verified else "  [UNVERIFIED defaults]"
            print(f"{p.name:40s} pooling={p.pooling:5s} max_len={p.max_len:<6d}{flag}\n    {p.note}")
        return 0
    prof = PROFILES.get(args.model.casefold())
    if prof is None or not prof.repo:
        print(f"Unknown or non-downloadable model '{args.model}'. Use --list.", file=sys.stderr)
        return 1
    if not prof.verified:
        print(f"WARNING: '{prof.name}' defaults are unverified. Check pooling/prefix on the model card first.")
    target = settings.models_dir / prof.name
    try:
        fetch(HF.format(repo=prof.repo, path=prof.onnx_file), target / prof.onnx_file)
        fetch(HF.format(repo=prof.repo, path="tokenizer.json"), target / "tokenizer.json")
    except Exception as exc:                                          # noqa: BLE001
        print(f"Download failed: {exc}\nSee the note at the top of this file for the optimum export route.",
              file=sys.stderr)
        return 1
    print(f"Done. Model files are in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
