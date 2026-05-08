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
    assert payload["providers"][0]["semantic_candidate_count"] == 12
    assert payload["sports"][0]["target_reached"] is False


def test_lightweight_semantic_cache_completion_verifier_counts_coverage_proofs(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    _write_index(cache_dir, "betting:semantic_rules:index:manifests", ["manifest-sxbet"])
    _write_index(cache_dir, "betting:semantic_rules:index:normalized", ["selection-sxbet"])
    _write_index(
        cache_dir,
        "betting:semantic_rules:index:coverage_proofs",
        [f"proof-{index}" for index in range(12)],
    )
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
    for index in range(12):
        _write_cache_json(
            cache_dir,
            f"betting:semantic_rules:coverage:proof:proof-{index}",
            {
                "proof_id": f"proof-{index}",
                "universe": {"sport": "soccer"},
                "coverage_set": {"provider_scope": ["SXBET"]},
            },
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
    assert payload["providers"][0]["event_candidate_count"] == 0
    assert payload["providers"][0]["coverage_proof_count"] == 12
    assert payload["providers"][0]["semantic_candidate_count"] == 12
    assert payload["sports"][0]["coverage_proof_count"] == 12
    assert payload["sports"][0]["target_reached"] is False


def test_lightweight_semantic_cache_completion_verifier_counts_execution_safe_tier(
    tmp_path: Path,
):
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
        "safety_tier": "EXECUTION_SAFE",
        "sport": "soccer",
        "support": {"observed_count": 12, "providers": ["SXBET"]},
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
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["total_execution_safe_templates"] == 1
    assert payload["providers"][0]["execution_safe_template_count"] == 1


def test_lightweight_semantic_cache_completion_verifier_tolerates_torn_key_index(
    tmp_path: Path,
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    _write_index(cache_dir, "betting:semantic_rules:index:manifests", ["manifest-sxbet"])
    _write_index(cache_dir, "betting:semantic_rules:index:normalized", ["selection-sxbet"])
    _write_index(cache_dir, "betting:semantic_rules:index:template_candidates", ["template-sxbet"])
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
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:template:candidate:template-sxbet",
        {
            "execution_safe": False,
            "safety_tier": "TOPOLOGY_SAFE",
            "sport": "soccer",
            "support": {"observed_count": 12, "providers": ["SXBET"]},
        },
    )
    (cache_dir / "keys.json").write_text("{", encoding="utf-8")

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


def test_lightweight_semantic_cache_completion_verifier_reports_corrupt_records(
    tmp_path: Path,
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    _write_index(cache_dir, "betting:semantic_rules:index:manifests", ["manifest-sxbet"])
    _write_index(
        cache_dir,
        "betting:semantic_rules:index:normalized",
        ["selection-sxbet", "selection-corrupt"],
    )
    _write_index(cache_dir, "betting:semantic_rules:index:template_candidates", ["template-sxbet"])
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
    _cache_path(
        cache_dir,
        "betting:semantic_rules:normalized:selection-corrupt",
    ).write_bytes(b"\x1f\x8bnot-valid-gzip")
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:template:candidate:template-sxbet",
        {
            "execution_safe": False,
            "safety_tier": "TOPOLOGY_SAFE",
            "sport": "soccer",
            "support": {"observed_count": 12, "providers": ["SXBET"]},
        },
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
    assert payload["load_error_count"] == 1
    assert payload["load_errors"][0]["key"] == (
        "betting:semantic_rules:normalized:selection-corrupt"
    )


def test_lightweight_semantic_cache_completion_verifier_tolerates_runtime_sparse_sport(
    tmp_path: Path,
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    status_file = tmp_path / "status.json"

    _write_index(cache_dir, "betting:semantic_rules:index:manifests", ["manifest-cloudbet"])
    _write_index(cache_dir, "betting:semantic_rules:index:normalized", ["selection-cloudbet"])
    _write_index(
        cache_dir,
        "betting:semantic_rules:index:template_candidates",
        ["template-cloudbet"],
    )
    _write_index(cache_dir, "betting:semantic_rules:index:template_promoted", ["template-cloudbet"])
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:manifest:manifest-cloudbet",
        {"provider": "CLOUDBET"},
    )
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:normalized:selection-cloudbet",
        {"provider": "CLOUDBET", "selection": {"sport": "american_football"}},
    )
    template = {
        "execution_safe": True,
        "safety_tier": "EXECUTION_SAFE",
        "sport": "american_football",
        "support": {
            "observed_count": 3,
            "providers": ["CLOUDBET"],
        },
    }
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:template:candidate:template-cloudbet",
        template,
    )
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:template:promoted:template-cloudbet",
        template,
    )
    status_file.write_text(
        json.dumps(
            {
                "semanticCache": {
                    "providerCorpusCoverage": {
                        "CLOUDBET": {
                            "sparse_sports": ["american-football"],
                            "sports": {
                                "american-football": {
                                    "selection_count": 6,
                                    "event_count": 1,
                                    "sparse": True,
                                },
                            },
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/deploy/strategy_nodes/verify_semantic_cache_completion.py",
            "--cache-dir",
            str(cache_dir),
            "--runtime-status-file",
            str(status_file),
            "--required-provider",
            "CLOUDBET",
            "--target-sport",
            "american_football",
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
    assert payload["sports"][0]["sparse_runtime_tolerated"] is True
    assert payload["sports"][0]["sparse_providers"] == ["CLOUDBET"]


def test_lightweight_semantic_cache_completion_verifier_stays_strict_without_runtime_status(
    tmp_path: Path,
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    _write_index(cache_dir, "betting:semantic_rules:index:manifests", ["manifest-cloudbet"])
    _write_index(cache_dir, "betting:semantic_rules:index:normalized", ["selection-cloudbet"])
    _write_index(
        cache_dir,
        "betting:semantic_rules:index:template_candidates",
        ["template-cloudbet"],
    )
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:manifest:manifest-cloudbet",
        {"provider": "CLOUDBET"},
    )
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:normalized:selection-cloudbet",
        {"provider": "CLOUDBET", "selection": {"sport": "american_football"}},
    )
    _write_cache_json(
        cache_dir,
        "betting:semantic_rules:template:candidate:template-cloudbet",
        {
            "execution_safe": True,
            "safety_tier": "EXECUTION_SAFE",
            "sport": "american_football",
            "support": {"observed_count": 3, "providers": ["CLOUDBET"]},
        },
    )

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/deploy/strategy_nodes/verify_semantic_cache_completion.py",
            "--cache-dir",
            str(cache_dir),
            "--required-provider",
            "CLOUDBET",
            "--target-sport",
            "american_football",
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

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["passed"] is False
    assert payload["sports"][0]["blockers"] == ["below_min_candidate_count"]
