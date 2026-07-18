"""
CLI: dry-run the shard allocation or emit deployable manifests.

``dry-run`` prints the measured weight table, the packed bins with
over-capacity warnings, the diff against the current fleet manifests (by
sport-set), and the exact deploy commands. ``emit`` additionally writes the
manifests and validates each one through the repo manifest loader, the
trading-node config builder, and the live-pilot lint script.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tools.shardplan import collect
from tools.shardplan import emit
from tools.shardplan.pack import Bin
from tools.shardplan.pack import PackResult
from tools.shardplan.pack import pack


DEFAULT_CAPACITY = 2000
DEFAULT_DEPLOY_DIR = Path("deploy/strategy_nodes/betting_arbitrage")
DEPLOY_WORKFLOW = "strategy-node-release.yml"
DEPLOY_REPO = "antonga23/cloudbet-market-maker"


def _sxbet_id_to_sport() -> dict[int, str]:
    from nautilus_trader.adapters.sxbet.constants import SXBET_SPORT_IDS

    return dict(SXBET_SPORT_IDS)


def manifest_sport_set(payload: dict[str, Any], id_to_sport: dict[int, str]) -> frozenset[str]:
    """
    Return the sport scope of an existing manifest: the union of every
    venue's ``sport_keys`` plus SXBET ``sport_ids`` mapped back to sport
    names.
    """
    sports: set[str] = set()
    for venue in payload.get("venues") or []:
        if not isinstance(venue, dict):
            continue
        for key in venue.get("sport_keys") or []:
            sports.add(str(key))
        for sport_id in venue.get("sport_ids") or []:
            try:
                mapped = id_to_sport.get(int(sport_id))
            except (TypeError, ValueError):
                mapped = None
            if mapped is not None:
                sports.add(mapped)
    return frozenset(sports)


def _is_shard_like(payload: dict[str, Any]) -> bool:
    venues = [venue for venue in payload.get("venues") or [] if isinstance(venue, dict)]
    return (
        bool(payload.get("validation_mode"))
        and len(venues) >= 2
        and all(venue.get("sport_keys") or venue.get("sport_ids") for venue in venues)
    )


def existing_sport_scopes(deploy_dir: Path) -> dict[str, frozenset[str]]:
    """
    Sport scopes of the shard-like manifests already in the deploy dir:
    validation-mode, multi-venue manifests where every venue is sport-scoped.
    Live-pilot and single-venue validation manifests are not shardplan's to
    retire.
    """
    id_to_sport = _sxbet_id_to_sport()
    scopes: dict[str, frozenset[str]] = {}
    for path in sorted(deploy_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not _is_shard_like(payload):
            continue
        sports = manifest_sport_set(payload, id_to_sport)
        if sports:
            scopes[path.name] = sports
    return scopes


def diff_against_fleet(
    bins: tuple[Bin, ...],
    deploy_dir: Path,
) -> tuple[dict[str, str], list[str]]:
    """
    Match bins to existing manifests by sport-set.

    Returns
    -------
    ``{bin_name: status}`` plus the existing sport-scoped manifests whose
    scope matches no bin (candidates to retire).

    """
    scopes = existing_sport_scopes(deploy_dir)
    statuses: dict[str, str] = {}
    matched: set[str] = set()
    for shard_bin in bins:
        match = next(
            (name for name, sports in sorted(scopes.items()) if sports == shard_bin.sport_set),
            None,
        )
        if match is None:
            statuses[shard_bin.name] = "NEW"
        else:
            statuses[shard_bin.name] = f"matches {match}"
            matched.add(match)
    unmatched = [name for name in sorted(scopes) if name not in matched]
    return statuses, unmatched


def deploy_command(manifest_path: Path, shard_bin: Bin) -> str:
    return (
        f"gh workflow run {DEPLOY_WORKFLOW} \\\n"
        f"  --repo {DEPLOY_REPO} \\\n"
        f"  -f manifest_path={manifest_path} \\\n"
        f"  -f deploy_enabled=true \\\n"
        f"  -f container_name={emit.container_name_for(shard_bin)} \\\n"
        f"  -f image_transport=archive"
    )


def format_weight_table(table: collect.WeightTable) -> str:
    lines = [f"{'sport':<20} {'total':>7} {'starvation':>11}  venues"]
    for sport in sorted(table, key=lambda name: (-table[name].total, name)):
        weight = table[sport]
        venues = " ".join(f"{venue}={count}" for venue, count in sorted(weight.venues.items()))
        lines.append(f"{sport:<20} {weight.total:>7} {weight.starvation:>11}  {venues}")
    return "\n".join(lines)


def format_bins(result: PackResult) -> str:
    lines = [f"capacity per bin: {result.capacity}"]
    for shard_bin in result.bins:
        utilization = 100.0 * shard_bin.weight / shard_bin.capacity
        flags = []
        if shard_bin.dedicated:
            flags.append("dedicated")
        if shard_bin.over_capacity:
            flags.append("OVER-CAPACITY: exceeds bin capacity, needs a league-level split")
        suffix = f"  [{'; '.join(flags)}]" if flags else ""
        lines.append(
            f"  {shard_bin.name:<34} weight={shard_bin.weight:>5} "
            f"({utilization:5.1f}%)  sports={','.join(shard_bin.sports)}{suffix}",
        )
    if result.dropped:
        lines.append(f"  dropped (zero weight, out of season): {', '.join(result.dropped)}")
    return "\n".join(lines)


def build_allocation(args: argparse.Namespace) -> tuple[collect.WeightTable, PackResult]:
    table: collect.WeightTable = {}
    if args.nodes_root is not None:
        table = collect.collect_weights(args.nodes_root)
    if args.static_weights is not None:
        table = collect.merge_weights(table, collect.load_static_weights(args.static_weights))
    if not table:
        raise SystemExit("No weights collected; pass --nodes-root and/or --static-weights")
    totals = {sport: weight.total for sport, weight in table.items()}
    return table, pack(totals, args.capacity)


def _report(args: argparse.Namespace, manifest_dir: Path) -> tuple[PackResult, list[str]]:
    table, result = build_allocation(args)
    print("== Sport weights ==")
    print(format_weight_table(table))
    print()
    print("== Bins (first-fit-decreasing, whole-sport atomic) ==")
    print(format_bins(result))
    print()
    print(f"== Diff vs {args.deploy_dir} (by sport-set) ==")
    statuses, unmatched = diff_against_fleet(result.bins, args.deploy_dir)
    for shard_bin in result.bins:
        print(f"  {shard_bin.name:<34} {statuses[shard_bin.name]}")
    for name in unmatched:
        print(f"  {name:<34} no matching bin (candidate to retire)")
    print()
    print("== Deploy commands (ops runs the existing workflow per manifest) ==")
    for shard_bin in result.bins:
        print(deploy_command(manifest_dir / f"{shard_bin.name}.json", shard_bin))
        print()
    return result, unmatched


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--nodes-root", type=Path, help="Fleet nodes root with status payloads")
    parser.add_argument(
        "--static-weights",
        type=Path,
        help="Static weights JSON (overrides measured sports)",
    )
    parser.add_argument("--capacity", type=int, default=DEFAULT_CAPACITY)
    parser.add_argument("--deploy-dir", type=Path, default=DEFAULT_DEPLOY_DIR)
    parser.add_argument("--template", type=Path, default=emit.TEMPLATE_PATH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shardplan", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser("dry-run", help="Print the allocation without writing")
    _add_common_args(dry_run)

    emit_cmd = subparsers.add_parser("emit", help="Write and validate shard manifests")
    _add_common_args(emit_cmd)
    emit_cmd.add_argument("--out", type=Path, required=True, help="Output manifest directory")

    args = parser.parse_args(argv)

    if args.command == "dry-run":
        _report(args, args.deploy_dir)
        return 0

    result, _ = _report(args, args.out)
    paths = emit.emit_manifests(result.bins, args.out, template_path=args.template)
    print("== Emitted manifests (validated: load + build + lint) ==")
    for path in paths:
        summary = emit.validate_manifest_file(path, template_path=args.template)
        print(
            f"  {path} venues={','.join(summary['venues'])} "
            f"data_clients={summary['data_clients']} exec_clients={summary['exec_clients']}",
        )
    return 0
