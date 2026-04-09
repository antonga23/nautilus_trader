#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZipFile


def is_extension_member(member: str) -> bool:
    return member.endswith((".so", ".pyd"))


def main(argv: list[str]) -> int:
    wheel_paths = [Path(arg) for arg in argv[1:]]
    if not wheel_paths:
        print(
            "Usage: extract_compiled_extensions_from_wheel.py <wheel> [<wheel> ...]",
            file=sys.stderr,
        )
        return 64

    extracted = []
    for wheel_path in wheel_paths:
        if not wheel_path.exists():
            print(f"Wheel not found: {wheel_path}", file=sys.stderr)
            return 1

        with ZipFile(wheel_path) as wheel:
            members = [member for member in wheel.namelist() if is_extension_member(member)]
            if not members:
                continue

            for member in members:
                wheel.extract(member, Path.cwd())
                extracted.append(member)

    if not extracted:
        print(
            "No compiled extensions were found in the provided wheel(s); "
            "refusing to run tests against an incomplete source tree.",
            file=sys.stderr,
        )
        return 1

    print(f"Extracted {len(extracted)} compiled extension(s):")
    for member in extracted:
        print(f"- {member}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
