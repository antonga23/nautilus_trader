#!/usr/bin/env bash
set -euo pipefail

mkdir -p tests/results/custom tests/results/full

custom_workers="${PYTEST_XDIST_WORKERS:-6}"
full_suite_workers="${FULL_SUITE_PYTEST_XDIST_WORKERS:-$custom_workers}"

bash scripts/ci/run_pytest_with_reporting.sh \
  "Custom logic tests" \
  tests/results/custom/junit.xml \
  tests/results/custom/pytest.log \
  tests/unit/adapters \
  tests/unit/strategies \
  tests/integration \
  --tb=line \
  -ra \
  -n "$custom_workers" \
  --dist=loadgroup \
  --maxfail=10 \
  --cov=nautilus_trader/adapters/betting \
  --cov=nautilus_trader/adapters/blackbet \
  --cov=nautilus_trader/adapters/easybet \
  --cov=nautilus_trader/adapters/tenbet \
  --cov=nautilus_trader/adapters/sxbet \
  --cov=nautilus_trader/adapters/wsb \
  --cov=nautilus_trader/examples/strategies/betting_arbitrage.py \
  --cov-report=xml:tests/results/custom/coverage.xml \
  --cov-report=term-missing

bash scripts/ci/run_pytest_with_reporting.sh \
  "Full Python suite" \
  tests/results/full/junit.xml \
  tests/results/full/pytest.log \
  --ignore=tests/performance_tests \
  --ignore=tests/integration_tests/infrastructure/test_cache_database_postgres.py \
  --tb=line \
  -ra \
  -n "$full_suite_workers" \
  --dist=loadgroup \
  --maxfail=50 \
  --durations=25 \
  --durations-min=5.0
