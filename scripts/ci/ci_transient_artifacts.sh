#!/usr/bin/env bash
set -euo pipefail

python_bin="${CI_TRANSIENT_PYTHON_BIN:-$(command -v python3 || true)}"
if [[ -z "$python_bin" ]]; then
  echo "python3 is required for transient CI artifact storage" >&2
  exit 69
fi

if "$python_bin" -c 'import botocore' > /dev/null 2>&1; then
  exec "$python_bin" scripts/ci/ci_transient_artifacts.py "$@"
fi

botocore_version="${CI_TRANSIENT_BOTOCORE_VERSION:-1.40.58}"
if command -v uv > /dev/null 2>&1; then
  exec uv run --with "botocore==${botocore_version}" --no-project \
    python scripts/ci/ci_transient_artifacts.py "$@"
fi

export CI_AWSCLI_IGNORE_SYSTEM="${CI_AWSCLI_IGNORE_SYSTEM:-true}"
aws_bin_dir="$(bash scripts/ci/ensure_aws_cli.sh)"
aws_python="${aws_bin_dir}/python"
if [[ ! -x "$aws_python" ]]; then
  echo "Bundled AWS Python runtime not found: ${aws_python}" >&2
  exit 69
fi

exec "$aws_python" scripts/ci/ci_transient_artifacts.py "$@"
