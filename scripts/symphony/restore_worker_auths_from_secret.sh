#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
config_path="$repo_root/scripts/symphony/workers.json"
secret_id="${1:-${AGENT_SECRET_ID:-cloudbet-market-maker/credentials}}"

if [ ! -f "$config_path" ]; then
  echo "Missing worker config: $config_path" >&2
  exit 1
fi

if ! command -v aws > /dev/null 2>&1 || ! command -v jq > /dev/null 2>&1; then
  echo "aws CLI and jq are required" >&2
  exit 1
fi

secret_json="$(
  aws secretsmanager get-secret-value \
    --secret-id "$secret_id" \
    --query SecretString \
    --output text
)"

jq -c '.workers[] | select(.enabled != false)' "$config_path" | while read -r worker_json; do
  name="$(jq -r '.name' <<< "$worker_json")"
  user="$(jq -r '.user' <<< "$worker_json")"
  secret_key="$(jq -r '.secretKey // empty' <<< "$worker_json")"
  if [ -z "$secret_key" ]; then
    secret_key="CODEX_WORKER_AUTH_$(tr '[:lower:]-' '[:upper:]_' <<< "$name")_B64"
  fi
  auth_b64="$(jq -r --arg key "$secret_key" '.[$key] // empty' <<< "$secret_json")"
  if [ -z "$auth_b64" ]; then
    continue
  fi

  tmp_auth="$(mktemp)"
  printf '%s' "$auth_b64" | base64 --decode > "$tmp_auth"
  sudo install -d -o "$user" -g "$user" -m 700 "/home/$user/.codex"
  sudo install -o "$user" -g "$user" -m 600 "$tmp_auth" "/home/$user/.codex/auth.json"
  rm -f "$tmp_auth"
done
