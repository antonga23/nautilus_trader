#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path.cwd()
PATTERNS = [
    "Cargo.lock",
    "Cargo.toml",
    "pyproject.toml",
    "build.py",
    "capnp-version",
    "rust-toolchain.toml",
    "crates/**",
    "nautilus_trader/**",
    "schema/**",
]

hasher = hashlib.sha256()
files: list[Path] = []
for pattern in PATTERNS:
    files.extend(path for path in ROOT.glob(pattern) if path.is_file())

for path in sorted(set(files)):
    rel = path.relative_to(ROOT).as_posix()
    hasher.update(rel.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(path.read_bytes())
    hasher.update(b"\0")

print(hasher.hexdigest())
