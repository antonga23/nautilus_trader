#!/usr/bin/env bash
set -euo pipefail

awscli_version="${CI_AWSCLI_VERSION:-1.44.86}"

if [[ "${CI_AWSCLI_IGNORE_SYSTEM:-false}" != "true" ]] && command -v aws > /dev/null 2>&1; then
  dirname "$(command -v aws)"
  exit 0
fi

if ! command -v python3 > /dev/null 2>&1; then
  echo "python3 is required to bootstrap awscli" >&2
  exit 69
fi

if [[ -n "${CI_AWSCLI_CACHE_DIR:-}" ]]; then
  cache_root="${CI_AWSCLI_CACHE_DIR}"
else
  workspace="${GITHUB_WORKSPACE:-$PWD}"
  cache_root="$(dirname "$workspace")/.ci-cache/awscli/${awscli_version}"
fi

venv_dir="${cache_root}/venv"
aws_bin="${venv_dir}/bin/aws"
lock_dir="${cache_root}.lock"

if [[ -x "$aws_bin" ]] && ! "$aws_bin" --version > /dev/null 2>&1; then
  rm -rf "$cache_root"
fi

if [[ ! -x "$aws_bin" ]]; then
  mkdir -p "$(dirname "$cache_root")"

  acquired_lock=false
  for _ in {1..120}; do
    if mkdir "$lock_dir" 2> /dev/null; then
      acquired_lock=true
      break
    fi
    if [[ -x "$aws_bin" ]]; then
      break
    fi
    sleep 2
  done

  if [[ "$acquired_lock" != "true" && ! -x "$aws_bin" ]]; then
    echo "Timed out waiting for awscli cache lock: ${lock_dir}" >&2
    exit 75
  fi

  if [[ "$acquired_lock" == "true" ]]; then
    cleanup_lock() {
      rmdir "$lock_dir" 2> /dev/null || true
    }
    trap cleanup_lock EXIT

    if [[ ! -x "$aws_bin" ]]; then
      rm -rf "$cache_root"
      python3 -m venv "${venv_dir}"
      "${venv_dir}/bin/python" -m pip install --upgrade pip >&2
      "${venv_dir}/bin/python" -m pip install "awscli==${awscli_version}" >&2
    fi
  fi
fi

if [[ ! -x "$aws_bin" ]]; then
  echo "awscli bootstrap did not produce ${aws_bin}" >&2
  exit 69
fi

dirname "$aws_bin"
