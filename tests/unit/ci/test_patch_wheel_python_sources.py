# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for CI wheel patching helpers.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import csv
import importlib.util
import io
from pathlib import Path
import zipfile


SCRIPT_PATH = Path("scripts/ci/patch_wheel_python_sources.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("patch_wheel_python_sources", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_patch_wheel_python_sources_replaces_package_payload_and_record(tmp_path):
    root = tmp_path / "repo"
    package = root / "nautilus_trader"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 'patched'\n")
    (package / "module.pyi").write_text("VALUE: str\n")
    dist = root / "dist"
    dist.mkdir()
    wheel_path = dist / "nautilus_trader-0.0.0-py3-none-any.whl"
    record_path = "nautilus_trader-0.0.0.dist-info/RECORD"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("nautilus_trader/__init__.py", "VALUE = 'old'\n")
        wheel.writestr(record_path, f"nautilus_trader/__init__.py,,\n{record_path},,\n")

    module = _load_module()
    patched_count = module.patch_wheel(wheel_path, root=root)

    assert patched_count == 2
    with zipfile.ZipFile(wheel_path, "r") as wheel:
        assert wheel.read("nautilus_trader/__init__.py") == b"VALUE = 'patched'\n"
        assert wheel.read("nautilus_trader/module.pyi") == b"VALUE: str\n"
        record = wheel.read(record_path).decode("utf-8")

    rows = list(csv.reader(io.StringIO(record)))
    record_rows = {row[0]: row for row in rows}
    assert record_rows["nautilus_trader/__init__.py"][1].startswith("sha256=")
    assert record_rows["nautilus_trader/__init__.py"][2] == str(len("VALUE = 'patched'\n"))
    assert record_rows["nautilus_trader/module.pyi"][1].startswith("sha256=")
    assert record_rows[record_path][1:] == ["", ""]
