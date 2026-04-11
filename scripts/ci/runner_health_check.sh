#!/usr/bin/env bash
set -euo pipefail

runner_root="${ACTIONS_RUNNER_ROOT:-/home/ubuntu/actions-runner}"
runner_diag_root="${RUNNER_DIAG_ROOT:-$runner_root/_diag}"
runner_work_root="${RUNNER_WORK_ROOT:-$runner_root/_work}"
runner_local_cache_root="${RUNNER_LOCAL_CACHE_ROOT:-$runner_work_root/.ci-cache}"
workspace_root="${SYMPHONY_WORKSPACE_ROOT:-/srv/symphony/workspaces}"
control_repo_root="${SYMPHONY_CONTROL_REPO_ROOT:-/srv/symphony/control-repo}"

echo "== Filesystem =="
df -h /
echo

echo "== Top-level workspace usage =="
du -xhd1 "$runner_work_root" 2> /dev/null | sort -h || true
echo

echo "== Runner diag =="
du -sh "$runner_diag_root" 2> /dev/null || true
echo

echo "== Runner local caches =="
du -xhd2 "$runner_local_cache_root" 2> /dev/null | sort -h || true
echo

echo "== Docker =="
docker ps -a --format 'table {{.ID}}\t{{.Status}}\t{{.Names}}' 2> /dev/null || true
echo

echo "== Services =="
for service in \
  actions.runner.antonga23-cloudbet-market-maker.EC2-Runner.service \
  control-plane.service \
  actions-runner-hygiene.timer; do
  printf '%s\t%s\n' "$service" "$(systemctl is-active "$service" 2> /dev/null || true)"
done
echo

echo "== Symphony workspaces =="
du -xhd1 "$workspace_root" 2> /dev/null | sort -h || true
echo

echo "== Control repo derived artifacts =="
for artifact in .cache .mypy_cache .pytest_cache .ruff_cache .venv artifacts dist target; do
  du -sh "$control_repo_root/$artifact" 2> /dev/null || true
done
