#!/usr/bin/env python3
"""
FileRuleCache write-throughput benchmark.

Continuous-experimentation harness for the semantic mine's storage hot path. The mine
persists thousands of artifacts through FileRuleCache, which fsync'd every record; on EBS
that dominates mine wall time. This times N record writes in the normal path vs inside
`bulk_writes()` (per-record fsync deferred to one directory fsync) and verifies the two
paths leave byte-identical cache content.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

from nautilus_trader.adapters.betting.semantics.store import FileRuleCache


def _run(cache_dir: Path, count: int, payload: bytes, *, bulk: bool) -> float:
    cache = FileRuleCache(cache_dir)
    keys = [f"betting:semantic_rules:candidate:{i}" for i in range(count)]
    start = time.monotonic()
    if bulk:
        with cache.bulk_writes():
            for key in keys:
                cache.add(key, payload)
    else:
        for key in keys:
            cache.add(key, payload)
    cache.flush_key_index()
    return time.monotonic() - start


def _content(cache_dir: Path, count: int) -> dict[str, bytes]:
    cache = FileRuleCache(cache_dir)
    return {
        f"betting:semantic_rules:candidate:{i}": cache.get(
            f"betting:semantic_rules:candidate:{i}",
        )
        for i in range(count)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5000)
    parser.add_argument("--payload-bytes", type=int, default=900)
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args(argv)

    payload = b"x" * args.payload_bytes
    with tempfile.TemporaryDirectory() as root:
        normal_dir = Path(root) / "normal"
        bulk_dir = Path(root) / "bulk"
        normal_secs = _run(normal_dir, args.count, payload, bulk=False)
        bulk_secs = _run(bulk_dir, args.count, payload, bulk=True)
        identical = _content(normal_dir, args.count) == _content(bulk_dir, args.count)

    metrics = {
        "count": args.count,
        "normalSecs": round(normal_secs, 3),
        "bulkSecs": round(bulk_secs, 3),
        "normalWritesPerSec": round(args.count / normal_secs) if normal_secs else None,
        "bulkWritesPerSec": round(args.count / bulk_secs) if bulk_secs else None,
        "speedup": round(normal_secs / bulk_secs, 1) if bulk_secs else None,
        "contentIdentical": identical,
    }
    print(json.dumps(metrics, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(metrics, indent=2))
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
