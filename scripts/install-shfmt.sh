#!/usr/bin/env bash
set -euo pipefail

version="${SHFMT_VERSION:-3.13.1}"
install_dir="${HOME}/.local/bin"
target="${install_dir}/shfmt"

if command -v shfmt > /dev/null 2>&1; then
  echo "shfmt already available: $(command -v shfmt)"
  exit 0
fi

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"

case "$arch" in
  x86_64 | amd64)
    arch="amd64"
    ;;
  aarch64 | arm64)
    arch="arm64"
    ;;
  *)
    echo "Unsupported architecture for shfmt install: $arch" >&2
    exit 1
    ;;
esac

case "$os" in
  linux | darwin) ;;
  *)
    echo "Unsupported OS for shfmt install: $os" >&2
    exit 1
    ;;
esac

mkdir -p "$install_dir"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

archive="shfmt_v${version}_${os}_${arch}"
url="https://github.com/mvdan/sh/releases/download/v${version}/${archive}"

curl -fsSL "$url" -o "${tmp_dir}/shfmt"
install -m 0755 "${tmp_dir}/shfmt" "$target"

echo "Installed shfmt to $target"
