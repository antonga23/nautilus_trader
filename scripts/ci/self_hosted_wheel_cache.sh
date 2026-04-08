#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <restore|save> <cache-key>" >&2
  exit 64
fi

action="$1"
cache_key="$2"
workspace_root="${RUNNER_WORKSPACE:-${GITHUB_WORKSPACE%/*}}"
cache_root="${SELF_HOSTED_WHEEL_CACHE_ROOT:-$workspace_root/.ci-cache/wheels}"
cache_dir="${cache_root}/${cache_key}"
dist_dir="${WHEEL_DIST_DIR:-${GITHUB_WORKSPACE:-$PWD}/dist}"

emit_cache_hit() {
  local value="$1"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "cache-hit=${value}" >> "$GITHUB_OUTPUT"
  fi
}

case "$action" in
  restore)
    if compgen -G "${cache_dir}/dist/*.whl" > /dev/null; then
      rm -rf "$dist_dir"
      mkdir -p "$dist_dir"
      cp -a "${cache_dir}/dist/." "$dist_dir/"
      echo "Restored self-hosted wheel cache from ${cache_dir}"
      emit_cache_hit "true"
    else
      echo "No self-hosted wheel cache found for ${cache_key}"
      emit_cache_hit "false"
    fi
    ;;

  save)
    if ! compgen -G "${dist_dir}/*.whl" > /dev/null; then
      echo "No wheel artifacts found in ${dist_dir}" >&2
      exit 1
    fi

    mkdir -p "$cache_root"
    tmp_dir="$(mktemp -d "${cache_root}/.${cache_key}.tmp.XXXXXX")"
    mkdir -p "${tmp_dir}/dist"
    cp -a "${dist_dir}/." "${tmp_dir}/dist/"
    printf 'saved_at=%s\ngit_sha=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "${GITHUB_SHA:-unknown}" > "${tmp_dir}/metadata.env"
    rm -rf "$cache_dir"
    mv "$tmp_dir" "$cache_dir"
    echo "Saved self-hosted wheel cache to ${cache_dir}"
    ;;

  *)
    echo "Unknown action: ${action}" >&2
    exit 64
    ;;
esac
