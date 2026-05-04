---
name: semantic-rule-mining-completion
description: Use before claiming semantic betting rule mining, market semantics, provider corpus coverage, or OpportunityGraph matcher-rule work is complete. Verifies Cloudbet, SXBET, and Polymarket coverage, per-sport candidate gates, promotion blockers, runner-aware GCP execution, and durable logs.
---

# Semantic Rule Mining Completion

Use this skill before saying semantic rule mining work is done.

If the user explicitly defers a provider, keep that provider implemented but out
of the current completion gate. In that case, pass the active provider set
explicitly to `report-coverage` and `verify-completion` instead of relying on
the default required-provider list.

## Required Distinctions

- `event candidates`: discovered event-scoped relationships. These satisfy the candidate-count gate.
- `template candidates`: reusable generalized relationships. These support future matching.
- `promoted templates`: strict runtime-consumable templates. Zero promotions does not mean zero candidates.
- `execution-safe templates`: promoted `COMPLEMENTARY_COVERAGE` templates with no void, partial, or unknown settlement.

## Runner-Aware GCP Discipline

- Use `gcp-account-guard` before any GCP command.
- Target VM: `semantic-rule-miner-20260426` in project `shining-sol-493421-h6`.
- The VM also hosts the runner. Never stop, restart, teardown, or reconfigure the VM or runner service.
- Run mining in a separate semantic work directory and cache path.
- For any remote job expected to exceed 60 seconds, use the `background-monitor` skill and write logs under `artifacts/monitors/`.
- Before a heavy refresh, check runner activity read-only. If active, run only lightweight `report-coverage`/`verify-completion` or use a background monitor to wait for remote command completion.

## Completion Workflow

1. Refresh all required providers into the same durable cache:

```sh
.venv/bin/python scripts/betting/semantic_rule_mining.py refresh-corpus \
  --provider all \
  --sports soccer basketball tennis american_football ice_hockey baseball \
  --initial-window-seconds 86400 \
  --max-window-days 7 \
  --min-events-per-sport 1 \
  --include-past-on-sparse \
  --limit 50 \
  --instrument-limit 1000 \
  --market-discovery-limit 1000 \
  --cache-dir "$SEMANTIC_RULE_CACHE_DIR"
```

2. Mine and generalize:

```sh
.venv/bin/python scripts/betting/semantic_rule_mining.py mine-candidates \
  --cache-dir "$SEMANTIC_RULE_CACHE_DIR"
.venv/bin/python scripts/betting/semantic_rule_mining.py generalize-templates \
  --cache-dir "$SEMANTIC_RULE_CACHE_DIR"
```

3. Report counts and verify gates:

```sh
.venv/bin/python scripts/betting/semantic_rule_mining.py report-coverage \
  --cache-dir "$SEMANTIC_RULE_CACHE_DIR"
.venv/bin/python scripts/betting/semantic_rule_mining.py verify-completion \
  --cache-dir "$SEMANTIC_RULE_CACHE_DIR" \
  --min-candidates 10 \
  --target-candidates 20
```

## Pass Criteria

The task is not complete unless `verify-completion` exits `0` and reports:

- Required providers present for the active scope. Default scope is
  `CLOUDBET`, `SXBET`, `POLYMARKET`, but if the user explicitly defers a
  provider then that provider must be omitted from the command line and from the
  pass criteria for this run.
- Each required provider has a manifest, normalized selections, and event candidates.
- Target sports `soccer`, `basketball`, `tennis`, `american_football`, `ice_hockey`, and `baseball` have at least 10 event candidates each.
- The report shows progress toward 20 event candidates per target sport.
- Promotion blockers are listed separately from candidate counts.

If the verifier fails, continue refreshing wider windows, larger limits, and additional provider data until it passes or the blocker is a real provider/API constraint documented in the final status.
