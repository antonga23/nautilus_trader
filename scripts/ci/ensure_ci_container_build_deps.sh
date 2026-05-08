#!/usr/bin/env bash
set -euo pipefail

required_commands=(
  gcc
  g++
  git
  make
  pkg-config
  psql
)

missing=false
for cmd in "${required_commands[@]}"; do
  if ! command -v "$cmd" > /dev/null 2>&1; then
    missing=true
    break
  fi
done

if [[ "$missing" == false ]]; then
  echo "CI container build dependencies already available"
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Missing build dependencies and not running as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

prefer_https_ubuntu_sources() {
  local source_file
  shopt -s nullglob
  for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    if [[ -f "$source_file" ]]; then
      sed -i \
        -e 's#http://archive.ubuntu.com/ubuntu#https://archive.ubuntu.com/ubuntu#g' \
        -e 's#http://security.ubuntu.com/ubuntu#https://security.ubuntu.com/ubuntu#g' \
        "$source_file"
    fi
  done
  shopt -u nullglob
}

apt_with_retry() {
  local description="$1"
  shift
  local attempt=1
  local max_attempts=5

  while true; do
    if "$@"; then
      return 0
    fi

    if [[ "$attempt" -ge "$max_attempts" ]]; then
      echo "${description} failed after ${attempt} attempts" >&2
      return 1
    fi

    sleep $((attempt * 5))
    rm -rf /var/lib/apt/lists/*
    attempt=$((attempt + 1))
  done
}

prefer_https_ubuntu_sources
apt_with_retry "apt-get update" apt-get update -o Acquire::Retries=5
apt_with_retry "apt-get install" \
  apt-get install -y --no-install-recommends \
  build-essential \
  pkg-config \
  postgresql-client \
  python3-dev \
  libpython3-dev \
  git \
  make \
  ca-certificates \
  -o Acquire::Retries=5
rm -rf /var/lib/apt/lists/*
