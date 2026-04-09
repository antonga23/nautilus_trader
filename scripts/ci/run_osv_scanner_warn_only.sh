#!/usr/bin/env bash
set -euo pipefail

tmp_output="$(mktemp)"
trap 'rm -f "$tmp_output"' EXIT

set +e
osv-scanner "$@" > "$tmp_output" 2>&1
status=$?
set -e

python3 - "$tmp_output" << 'PY'
from __future__ import annotations

import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
filtered: list[str] = []
skip_unused_ignores = False

for line in lines:
    if line.startswith("osv-scanner.toml has unused ignores:"):
        skip_unused_ignores = True
        continue
    if skip_unused_ignores:
        if line.startswith(" - "):
            continue
        skip_unused_ignores = False
    filtered.append(line)

print("\n".join(filtered))
PY

if [[ $status -ne 0 ]]; then
  echo "osv-scanner exited with status ${status}, continuing because this hook is informational" >&2
fi
