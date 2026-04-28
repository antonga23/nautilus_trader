#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 /path/to/service-account.json [secret-id]" >&2
  exit 1
fi

json_path="$1"
secret_id="${2:-${AGENT_SECRET_ID:-cloudbet-market-maker/credentials}}"
secret_key="${GCP_SERVICE_ACCOUNT_SECRET_KEY:-GCP_SERVICE_ACCOUNT_JSON_B64}"

if [ ! -f "$json_path" ]; then
  echo "Missing JSON file: $json_path" >&2
  exit 1
fi

if ! command -v aws > /dev/null 2>&1 || ! command -v jq > /dev/null 2>&1; then
  echo "aws CLI and jq are required" >&2
  exit 1
fi

current_json_file="$(mktemp)"
updated_json_file="$(mktemp)"
service_account_b64_file="$(mktemp)"
trap 'rm -f "$current_json_file" "$updated_json_file" "$service_account_b64_file"' EXIT

aws secretsmanager get-secret-value \
  --secret-id "$secret_id" \
  --query SecretString \
  --output text > "$current_json_file"

base64 < "$json_path" | tr -d '\n' > "$service_account_b64_file"
jq --arg key "$secret_key" --rawfile value "$service_account_b64_file" \
  '.[$key] = $value' \
  "$current_json_file" > "$updated_json_file"

aws secretsmanager put-secret-value \
  --secret-id "$secret_id" \
  --secret-string "file://$updated_json_file" > /dev/null

echo "Persisted $secret_key to $secret_id" >&2
