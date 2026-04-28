#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 /path/to/gcloud-config-dir [secret-id]" >&2
  exit 1
fi

config_dir="$1"
secret_id="${2:-${AGENT_SECRET_ID:-cloudbet-market-maker/credentials}}"
secret_key="${GCP_GCLOUD_CONFIG_SECRET_KEY:-GCP_GCLOUD_CONFIG_TAR_B64}"
chunk_prefix="${GCP_GCLOUD_CONFIG_CHUNK_PREFIX:-cloudbet-market-maker/gcloud-config}"
chunk_size="${GCP_GCLOUD_CONFIG_CHUNK_SIZE:-50000}"

if [ ! -d "$config_dir" ]; then
  echo "Missing gcloud config dir: $config_dir" >&2
  exit 1
fi

if ! command -v aws > /dev/null 2>&1 || ! command -v jq > /dev/null 2>&1; then
  echo "aws CLI and jq are required" >&2
  exit 1
fi

tmp_bundle="$(mktemp)"
tmp_config_dir="$(mktemp -d)"
current_json_file="$(mktemp)"
updated_json_file="$(mktemp)"
config_b64_file="$(mktemp)"
chunk_dir="$(mktemp -d)"
trap 'rm -f "$tmp_bundle" "$current_json_file" "$updated_json_file" "$config_b64_file"; rm -rf "$tmp_config_dir" "$chunk_dir"' EXIT

for file_name in \
  active_config \
  access_tokens.db \
  application_default_credentials.json \
  config_sentinel \
  credentials.db \
  gce; do
  if [ -f "$config_dir/$file_name" ]; then
    cp -p "$config_dir/$file_name" "$tmp_config_dir/$file_name"
  fi
done

for dir_name in configurations legacy_credentials; do
  if [ -d "$config_dir/$dir_name" ]; then
    cp -R "$config_dir/$dir_name" "$tmp_config_dir/$dir_name"
  fi
done

tar -czf "$tmp_bundle" -C "$tmp_config_dir" .

aws secretsmanager get-secret-value \
  --secret-id "$secret_id" \
  --query SecretString \
  --output text > "$current_json_file"

base64 < "$tmp_bundle" | tr -d '\n' > "$config_b64_file"
config_b64_size="$(wc -c < "$config_b64_file" | tr -d ' ')"

if [ "$config_b64_size" -le "$chunk_size" ]; then
  jq --arg key "$secret_key" --rawfile value "$config_b64_file" \
    --arg chunkPrefixKey "GCP_GCLOUD_CONFIG_CHUNK_PREFIX" \
    --arg chunkCountKey "GCP_GCLOUD_CONFIG_CHUNK_COUNT" \
    --arg chunkShaKey "GCP_GCLOUD_CONFIG_B64_SHA256" \
    '.[$key] = $value | del(.[$chunkPrefixKey], .[$chunkCountKey], .[$chunkShaKey])' \
    "$current_json_file" > "$updated_json_file"
else
  split -b "$chunk_size" -d -a 3 "$config_b64_file" "$chunk_dir/part-"
  chunk_count=0
  for chunk_file in "$chunk_dir"/part-*; do
    chunk_secret_id="$chunk_prefix/part-$(printf '%03d' "$chunk_count")"
    if aws secretsmanager describe-secret --secret-id "$chunk_secret_id" > /dev/null 2>&1; then
      aws secretsmanager put-secret-value \
        --secret-id "$chunk_secret_id" \
        --secret-string "file://$chunk_file" > /dev/null
    else
      aws secretsmanager create-secret \
        --name "$chunk_secret_id" \
        --secret-string "file://$chunk_file" > /dev/null
    fi
    chunk_count=$((chunk_count + 1))
  done

  config_sha="$(
    python3 - "$config_b64_file" << 'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
  )"
  jq --arg key "$secret_key" \
    --arg chunkPrefix "$chunk_prefix" \
    --argjson chunkCount "$chunk_count" \
    --arg configSha "$config_sha" \
    'del(.[$key]) |
     .GCP_GCLOUD_CONFIG_CHUNK_PREFIX = $chunkPrefix |
     .GCP_GCLOUD_CONFIG_CHUNK_COUNT = $chunkCount |
     .GCP_GCLOUD_CONFIG_B64_SHA256 = $configSha' \
    "$current_json_file" > "$updated_json_file"
fi

aws secretsmanager put-secret-value \
  --secret-id "$secret_id" \
  --secret-string "file://$updated_json_file" > /dev/null

echo "Persisted gcloud config auth material for $secret_id" >&2
