#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path.cwd()
# Python test jobs execute the source tree directly via PYTHONPATH.
# Only changes that alter compiled extensions or wheel build behavior
# should invalidate the cached wheel used to inject binary artifacts.
PATTERNS = [
    "Cargo.lock",
    "Cargo.toml",
    "pyproject.toml",
    "build.py",
    "capnp-version",
    "rust-toolchain.toml",
    "crates/**/*",
    "nautilus_trader/**/*.pyx",
    "nautilus_trader/**/*.pxd",
    "nautilus_trader/**/*.pxi",
    "nautilus_trader/**/*.h",
    "nautilus_trader/core/includes/**/*",
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
