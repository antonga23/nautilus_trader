#!/usr/bin/env python3
"""
Apply a deploy-time semantic-cache-mode override to a strategy-node manifest copy.

Used by the ``strategy-node-release`` deploy job to inject the optional
``semantic_cache_mode`` / ``semantic_cache_default_root`` dispatch inputs into the
deployed manifest copy only. Empty values leave the manifest's declared value
untouched (the node default remains ``fresh`` = always re-mine).

"""

from __future__ import annotations

import json
import sys


ALLOWED_MODES = ("fresh", "reuse", "default")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: apply_semantic_cache_override.py <manifest> <mode> <root>", file=sys.stderr)
        return 2
    manifest_path, mode, default_root = argv[1], argv[2].strip(), argv[3].strip()
    if mode and mode not in ALLOWED_MODES:
        print(f"semantic_cache_mode must be one of {ALLOWED_MODES}, got {mode!r}", file=sys.stderr)
        return 1
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    if mode:
        manifest["semantic_rule_cache_mode"] = mode
    if default_root:
        manifest["semantic_rule_cache_default_root"] = default_root
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    print(
        f"semantic cache override applied: mode={mode or '(manifest)'} "
        f"default_root={default_root or '(manifest)'}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
