#!/usr/bin/env bash
set -euo pipefail

export CI_AWSCLI_IGNORE_SYSTEM="${CI_AWSCLI_IGNORE_SYSTEM:-true}"
aws_bin_dir="$(bash scripts/ci/ensure_aws_cli.sh)"
aws_python="${aws_bin_dir}/python"

if [[ ! -x "$aws_python" ]]; then
  echo "Bundled AWS Python runtime not found: ${aws_python}" >&2
  exit 69
fi

exec "$aws_python" scripts/ci/ci_transient_artifacts.py "$@"
