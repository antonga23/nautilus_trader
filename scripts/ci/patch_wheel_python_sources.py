#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import zipfile


SOURCE_SUFFIXES = {".py", ".pyi"}


def _record_digest(payload: bytes) -> str:
    digest = hashlib.sha256(payload).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _record_line(path: str, payload: bytes | None) -> str:
    row = [path, "", ""]
    if payload is not None:
        row[1] = _record_digest(payload)
        row[2] = str(len(payload))
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="")
    writer.writerow(row)
    return buffer.getvalue()


def _source_payloads(root: Path) -> dict[str, bytes]:
    package_root = root / "nautilus_trader"
    payloads: dict[str, bytes] = {}
    for path in package_root.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        payloads[relative] = path.read_bytes()
    return payloads


def patch_wheel(wheel_path: Path, *, root: Path) -> int:
    source_payloads = _source_payloads(root)
    if not source_payloads:
        msg = f"No Python sources found under {root / 'nautilus_trader'}"
        raise RuntimeError(msg)

    with zipfile.ZipFile(wheel_path, "r") as source_wheel:
        existing_payloads = {
            info.filename: source_wheel.read(info.filename)
            for info in source_wheel.infolist()
            if not info.is_dir()
        }

    record_paths = [name for name in existing_payloads if name.endswith(".dist-info/RECORD")]
    if len(record_paths) != 1:
        msg = f"Expected exactly one wheel RECORD in {wheel_path}, found {record_paths}"
        raise RuntimeError(msg)
    record_path = record_paths[0]

    patched_payloads = dict(existing_payloads)
    patched_count = 0
    for name, payload in source_payloads.items():
        if patched_payloads.get(name) != payload:
            patched_payloads[name] = payload
            patched_count += 1

    record_lines = [
        _record_line(name, None if name == record_path else payload)
        for name, payload in sorted(patched_payloads.items())
        if name != record_path
    ]
    record_lines.append(_record_line(record_path, None))
    patched_payloads[record_path] = "\n".join(record_lines).encode("utf-8") + b"\n"

    with tempfile.NamedTemporaryFile(
        prefix=f".{wheel_path.name}.",
        suffix=".tmp",
        dir=wheel_path.parent,
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as patched_wheel:
            for name, payload in sorted(patched_payloads.items()):
                patched_wheel.writestr(name, payload)
        tmp_path.replace(wheel_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return patched_count


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: patch_wheel_python_sources.py <wheel-path>", file=sys.stderr)
        return 64
    wheel_path = Path(sys.argv[1]).resolve()
    if not wheel_path.is_file():
        print(f"Wheel not found: {wheel_path}", file=sys.stderr)
        return 66
    patched_count = patch_wheel(wheel_path, root=Path.cwd())
    print(f"patched_python_sources={patched_count} wheel={wheel_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
