#!/usr/bin/env bash
set -euo pipefail

required_commands=(
	gcc
	g++
	pkg-config
	psql
)

missing=false
for cmd in "${required_commands[@]}"; do
	if ! command -v "$cmd" >/dev/null 2>&1; then
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
apt-get update -o Acquire::Retries=5
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
