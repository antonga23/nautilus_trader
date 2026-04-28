#!/usr/bin/env bash
set -euo pipefail

secret_id="${1:-${AGENT_SECRET_ID:-cloudbet-market-maker/credentials}}"
output_path="${2:-/srv/symphony/gcp-service-account.json}"
gcloud_config_dir="${3:-/srv/symphony/gcloud-config}"
secret_key="${GCP_SERVICE_ACCOUNT_SECRET_KEY:-GCP_SERVICE_ACCOUNT_JSON_B64}"
gcloud_config_key="${GCP_GCLOUD_CONFIG_SECRET_KEY:-GCP_GCLOUD_CONFIG_TAR_B64}"

if ! command -v aws > /dev/null 2>&1 || ! command -v jq > /dev/null 2>&1; then
  echo "aws CLI and jq are required" >&2
  exit 1
fi

secret_json_file="$(mktemp)"
tmp_auth="$(mktemp)"
tmp_bundle="$(mktemp)"
tmp_config_b64="$(mktemp)"
trap 'rm -f "$secret_json_file" "$tmp_auth" "$tmp_bundle" "$tmp_config_b64"' EXIT

aws secretsmanager get-secret-value \
  --secret-id "$secret_id" \
  --query SecretString \
  --output text > "$secret_json_file"

auth_b64="$(jq -r --arg key "$secret_key" '.[$key] // empty' "$secret_json_file")"
jq -r --arg key "$gcloud_config_key" '.[$key] // empty' "$secret_json_file" > "$tmp_config_b64"

restored_any=0
if [ -n "$auth_b64" ]; then
  printf '%s' "$auth_b64" | base64 --decode > "$tmp_auth"
  install -d "$(dirname "$output_path")"
  install -m 600 "$tmp_auth" "$output_path"
  echo "Restored GCP service account to $output_path" >&2
  restored_any=1
fi

if [ -s "$tmp_config_b64" ]; then
  base64 --decode < "$tmp_config_b64" > "$tmp_bundle"
  install -d "$gcloud_config_dir"
  find "$gcloud_config_dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  tar -xzf "$tmp_bundle" -C "$gcloud_config_dir"
  chmod -R go-rwx "$gcloud_config_dir" 2> /dev/null || true
  echo "Restored gcloud config bundle to $gcloud_config_dir" >&2
  restored_any=1
else
  chunk_prefix="$(jq -r '.GCP_GCLOUD_CONFIG_CHUNK_PREFIX // empty' "$secret_json_file")"
  chunk_count="$(jq -r '.GCP_GCLOUD_CONFIG_CHUNK_COUNT // empty' "$secret_json_file")"
  expected_sha="$(jq -r '.GCP_GCLOUD_CONFIG_B64_SHA256 // empty' "$secret_json_file")"
  if [ -n "$chunk_prefix" ] && [ -n "$chunk_count" ] && [ "$chunk_count" -gt 0 ] 2> /dev/null; then
    : > "$tmp_config_b64"
    index=0
    while [ "$index" -lt "$chunk_count" ]; do
      chunk_secret_id="$chunk_prefix/part-$(printf '%03d' "$index")"
      aws secretsmanager get-secret-value \
        --secret-id "$chunk_secret_id" \
        --query SecretString \
        --output text >> "$tmp_config_b64"
      index=$((index + 1))
    done
    if [ -n "$expected_sha" ]; then
      actual_sha="$(
        python3 - "$tmp_config_b64" << 'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
      )"
      if [ "$actual_sha" != "$expected_sha" ]; then
        echo "Restored gcloud config checksum mismatch" >&2
        exit 1
      fi
    fi
    base64 --decode < "$tmp_config_b64" > "$tmp_bundle"
    install -d "$gcloud_config_dir"
    find "$gcloud_config_dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    tar -xzf "$tmp_bundle" -C "$gcloud_config_dir"
    chmod -R go-rwx "$gcloud_config_dir" 2> /dev/null || true
    echo "Restored chunked gcloud config bundle to $gcloud_config_dir" >&2
    restored_any=1
  fi
fi

if [ "$restored_any" -eq 0 ]; then
  echo "No GCP auth material found in $secret_id ($secret_key, $gcloud_config_key)" >&2
  exit 1
fi
