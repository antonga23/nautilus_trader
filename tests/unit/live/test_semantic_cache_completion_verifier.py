from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.bin"


def _write_cache_json(cache_dir: Path, key: str, payload: dict) -> None:
    path = _cache_path(cache_dir, key)
    path.write_bytes(
        gzip.compress(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            compresslevel=1,
        ),
    )
    key_index_path = cache_dir / "keys.json"
    key_index = json.loads(key_index_path.read_text()) if key_index_path.exists() else {}
    key_index[key] = path.name
    key_index_path.write_text(json.dumps(key_index, sort_keys=True, indent=2))


def _write_index(cache_dir: Path, key: str, items: list[str]) -> None:
    _write_cache_json(cache_dir, key, {"items": items})


def test_lightweight_semantic_cache_completion_verifier(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    _write_index(cache_dir, "betting:semantic_rules:index:manifests", ["manifest-sxbet"])
    _write_index(cache_dir, "betting:semantic_rules:index:normalized", ["selection-sxbet"])
    _write_index(cache_dir, "betting:semantic_rules:index:template_candidates", ["template-sxbet"])
    _write_index(cache_dir, "betting:semantic_rules:index:template_promoted", ["template-sxbet"])
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:manifest:manifest-sxbet",
        {"provider": "SXBET"},
    )
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:normalized:selection-sxbet",
        {"provider": "SXBET", "selection": {"sport": "soccer"}},
    )
    template = {
        "execution_safe": True,
        "safety_tier": "EXECUTION_SAFE",
        "sport": "soccer",
        "support": {
            "observed_count": 12,
            "providers": ["SXBET"],
        },
    }
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:template:candidate:template-sxbet",
        template,
    )
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:template:promoted:template-sxbet",
        template,
    )

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/deploy/strategy_nodes/verify_semantic_cache_completion.py",
            "--cache-dir",
            str(cache_dir),
            "--required-provider",
            "SXBET",
            "--target-sport",
            "soccer",
            "--min-candidates",
            "10",
            "--target-candidates",
            "20",
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["providers"][0]["event_candidate_count"] == 12
    assert payload["sports"][0]["target_reached"] is False
