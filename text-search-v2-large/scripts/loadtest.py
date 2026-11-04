"""HTTP latency check for the running API (the plan's gate: p95 < 1 s, warm).

    python -m app.start                                   # in another terminal
    python -m scripts.loadtest --requests 400 --concurrency 4
    python -m scripts.loadtest --queries eval/queries/dev.json --budget-ms 1000

Two phases are reported separately because they answer different questions:
  cold  every distinct query once, concurrently  -> includes query embedding (nothing cached yet)
  warm  the same queries again, shuffled         -> the embedding cache is hot; steady-state behaviour
The exit code is 1 when the warm p95 exceeds ``--budget-ms``, any request failed,
or any response was degraded. An unready service exits with code 2.
Client-side time includes HTTP and JSON; the server's own total is reported next to it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx
from app.runtime import run_async

BUILTIN = [
    "Raktha Vajram", "rakta vajram", "tamil horror movies", "movies starring karthi", "thriller movies before 2000",
    "scene where the villain executes a rival smuggler at the docks", "he says \"I will return\"",
    "movies like Kaattu Pey", "latest tamil movies", "3 action films", "kattu pey", "hindi crime drama",
    "a ghost haunting a village at night", "heist with diamonds", "bengali romantic period film",
]


def load_queries(path: Path | None) -> list[str]:
    if path is None:
        return list(BUILTIN)
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(dict.fromkeys(q["text"] for q in data))


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))]


async def run_phase(client: httpx.AsyncClient, url: str, queries: list[str], concurrency: int):
    sem = asyncio.Semaphore(concurrency)
    client_ms: list[float] = []
    server_ms: list[float] = []
    statuses: dict[str, int] = {}
    errors = 0

    async def one(q: str) -> None:
        nonlocal errors
        async with sem:
            t = time.perf_counter()
            try:
                r = await client.post(url, json={"query": q, "limit": 10})
                r.raise_for_status()
                body = r.json()
                if not isinstance(body, dict) or body.get("status") not in {
                        "ok", "empty", "approximate", "needs_clarification", "degraded"}:
                    raise ValueError("Invalid search response")
            except (httpx.HTTPError, ValueError):
                errors += 1
                return
            client_ms.append((time.perf_counter() - t) * 1000)
            server_ms.append(body.get("timings_ms", {}).get("total", 0.0))
            statuses[body["status"]] = statuses.get(body["status"], 0) + 1

    t0 = time.perf_counter()
    await asyncio.gather(*(one(q) for q in queries))
    return client_ms, server_ms, statuses, errors, time.perf_counter() - t0


def report(name: str, res) -> float:
    client_ms, server_ms, statuses, errors, wall = res
    p95 = pct(client_ms, 95)
    print(f"{name:5s} n={len(client_ms):4d} errors={errors}  client p50={pct(client_ms, 50):6.1f} p95={p95:6.1f} "
          f"p99={pct(client_ms, 99):6.1f} max={max(client_ms, default=float('nan')):6.1f} ms | "
          f"server p50={pct(server_ms, 50):6.1f} p95={pct(server_ms, 95):6.1f} ms | "
          f"{len(client_ms) / wall:5.1f} req/s | statuses {statuses}")
    return p95


async def main_async(args) -> int:
    base = args.url.rstrip("/")
    queries = load_queries(args.queries)
    rng = random.Random(args.seed)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            ready_response = await client.get(f"{base}/api/ready")
            ready_response.raise_for_status()
            ready = ready_response.json()
            if not isinstance(ready, dict) or ready.get("status") != "ready":
                raise ValueError("service is not ready")
        except (httpx.HTTPError, ValueError) as exc:
            print(f"Cannot reach {base}: {exc.__class__.__name__}. Start it with: python -m app.start", file=sys.stderr)
            return 2
        print(f"server: generation={ready.get('generation')} assets={ready.get('assets')} "
              f"dense={ready.get('dense')} calibrated={ready.get('dense_calibrated')}")
        url = f"{base}/api/search"
        cold = await run_phase(client, url, queries, args.concurrency)
        report("cold", cold)
        warm_queries = [rng.choice(queries) for _ in range(args.requests)]
        warm = await run_phase(client, url, warm_queries, args.concurrency)
        p95 = report("warm", warm)
    failed = cold[3] + warm[3]
    degraded = cold[2].get("degraded", 0) + warm[2].get("degraded", 0)
    ok = p95 <= args.budget_ms and failed == 0 and degraded == 0
    print(f"budget: warm p95 {p95:.1f} ms vs {args.budget_ms:.0f} ms -> {'PASS' if ok else 'FAIL'}"
          + (f" ({failed} failed requests)" if failed else "")
          + (f" ({degraded} degraded responses)" if degraded else ""))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8001")
    ap.add_argument("--requests", type=int, default=200, help="warm-phase request count")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--queries", type=Path, default=None, help="eval query file (uses its 'text' fields); default: built-in set")
    ap.add_argument("--budget-ms", type=float, default=1000.0)
    ap.add_argument("--seed", type=int, default=1)
    return run_async(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
