#!/usr/bin/env bash
set -euo pipefail

runner_image="${CI_RUNNER_IMAGE:-ghcr.io/antonga23/cloudbet-market-maker/ci-runner@sha256:ed46e808744508e5627f3f8d32fc3eff6b0c047129af03db70a8a6225346235b}"
workspace_root="${WORKSPACE_ROOT:-$PWD}"
cache_root="${PREFLIGHT_CACHE_ROOT:-$workspace_root/.cache/preflight-runner}"
network_name="bet5-preflight-$(date +%s)"
postgres_container="${network_name}-postgres"
redis_container="${network_name}-redis"
runner_home="$cache_root/home"
runner_workspace="$cache_root/workspace"

mkdir -p "$runner_home" "$runner_workspace"

cleanup() {
  docker rm -f "$postgres_container" "$redis_container" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! docker image inspect "$runner_image" >/dev/null 2>&1; then
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    echo "$GITHUB_TOKEN" | docker login ghcr.io -u "${GITHUB_ACTOR:-antonga23}" --password-stdin >/dev/null
  fi
  docker pull "$runner_image"
fi

docker network create "$network_name" >/dev/null

docker run -d --rm \
  --name "$postgres_container" \
  --network "$network_name" \
  -e POSTGRES_USER=nautilus \
  -e POSTGRES_PASSWORD=pass \
  -e POSTGRES_DB=nautilus \
  postgres@sha256:f30e3de0ac9cc938dac627ef2231099867c694b5f949fadb924c8c977428c399 >/dev/null

docker run -d --rm \
  --name "$redis_container" \
  --network "$network_name" \
  public.ecr.aws/docker/library/redis:7.4.5-alpine3.21@sha256:bb186d083732f669da90be8b0f975a37812b15e913465bb14d845db72a4e3e08 >/dev/null

docker run --rm \
  --name "${network_name}-runner" \
  --network "$network_name" \
  --user "$(id -u):$(id -g)" \
  -e HOME=/runner-home \
  -e GITHUB_WORKSPACE=/workspace \
  -e RUNNER_WORKSPACE=/runner-workspace \
  -e UV_PYTHON=3.12 \
  -e PYTEST_XDIST_WORKERS="${PYTEST_XDIST_WORKERS:-6}" \
  -e PYTHONPATH=/workspace \
  -e PGHOST="$postgres_container" \
  -e PGPASSWORD=pass \
  -e PGUSER=nautilus \
  -e PGDATABASE=nautilus \
  -e REDIS_HOST="$redis_container" \
  -e GITHUB_STEP_SUMMARY=/workspace/tests/results/preflight-summary.md \
  -v "$workspace_root:/workspace" \
  -v "$runner_home:/runner-home" \
  -v "$runner_workspace:/runner-workspace" \
  -w /workspace \
  "$runner_image" \
  bash -lc '
    set -euo pipefail
    git config --global --add safe.directory "$GITHUB_WORKSPACE"
    python3 --version
    uv --version
    git --version

    wheel_key="$(python3 scripts/ci/compute_test_wheel_cache_key.py)"
    bash scripts/ci/self_hosted_wheel_cache.sh restore "$wheel_key" || true

    uv sync --all-groups --all-extras --no-install-package nautilus_trader

    if ! compgen -G "dist/*.whl" > /dev/null; then
      BUILD_MODE=release uv build --wheel --python 3.12
      bash scripts/ci/self_hosted_wheel_cache.sh save "$wheel_key"
    fi

    uv pip install dist/*.whl
    python3 scripts/ci/extract_compiled_extensions_from_wheel.py dist/*.whl
    bash scripts/ci/initialize_database_schema.sh
    bash scripts/ci/run_python_test_suites.sh
  '
