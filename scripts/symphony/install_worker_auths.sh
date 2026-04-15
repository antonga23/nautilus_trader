#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
config_path="$repo_root/scripts/symphony/workers.json"
auth_root="${CODEX_WORKER_AUTH_ROOT:-$HOME/.codex-workers}"
secret_id="${AGENT_SECRET_ID:-cloudbet-market-maker/credentials}"

cd "$repo_root"
set -a
source .env
set +a

chmod 600 "$EC2_KEY_PATH"
ssh_opts=(
  -i "$EC2_KEY_PATH"
  -o StrictHostKeyChecking=no
)

decode_email() {
  python3 - "$1" << 'PY'
import base64
import json
import sys

def decode_payload(token: str):
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode())
    except Exception:
        return {}

obj = json.load(open(sys.argv[1]))
tokens = obj.get("tokens") or {}
payload = decode_payload(tokens.get("id_token") or "") or decode_payload(tokens.get("access_token") or "")
print(payload.get("email") or payload.get("https://api.openai.com/profile", {}).get("email") or "")
PY
}

upsert_secret_key() {
  local key="$1"
  local value="$2"
  local current_json
  current_json="$(
    aws secretsmanager get-secret-value \
      --region "$AWS_DEFAULT_REGION" \
      --secret-id "$secret_id" \
      --query SecretString \
      --output text
  )"
  local updated_json
  updated_json="$(jq --arg key "$key" --arg value "$value" '.[$key] = $value' <<< "$current_json")"
  aws secretsmanager put-secret-value \
    --region "$AWS_DEFAULT_REGION" \
    --secret-id "$secret_id" \
    --secret-string "$updated_json" > /dev/null
}

jq -c '.workers[] | select(.enabled != false)' "$config_path" | while read -r worker_json; do
  name="$(jq -r '.name' <<< "$worker_json")"
  user="$(jq -r '.user' <<< "$worker_json")"
  expected_email="$(jq -r '.email' <<< "$worker_json")"
  secret_key="$(jq -r '.secretKey // empty' <<< "$worker_json")"
  if [ -z "$secret_key" ]; then
    secret_key="CODEX_WORKER_AUTH_$(tr '[:lower:]-' '[:upper:]_' <<< "$name")_B64"
  fi
  worker_auth_dir="$auth_root/$name"
  auth_file="$worker_auth_dir/auth.json"
  config_file="$worker_auth_dir/config.toml"

  if [ ! -f "$auth_file" ]; then
    echo "Missing auth file for $name at $auth_file" >&2
    exit 1
  fi

  if [ ! -f "$config_file" ]; then
    cat > "$config_file" << 'CONFIG'
cli_auth_credentials_store = "file"
forced_login_method = "chatgpt"
CONFIG
  fi

  auth_email="$(decode_email "$auth_file")"
  if [ -z "$auth_email" ]; then
    echo "Could not determine login email for $name from $auth_file" >&2
    exit 1
  fi
  if [ "$auth_email" != "$expected_email" ]; then
    echo "Refusing to install $name: expected $expected_email but auth file is for $auth_email" >&2
    exit 1
  fi

  ssh "${ssh_opts[@]}" "$EC2_USER@$EC2_HOST" "sudo install -d -o $user -g $user -m 700 /home/$user/.codex"
  scp "${ssh_opts[@]}" "$auth_file" "$config_file" "$EC2_USER@$EC2_HOST:/tmp/"
  ssh "${ssh_opts[@]}" "$EC2_USER@$EC2_HOST" "sudo install -o $user -g $user -m 600 /tmp/auth.json /home/$user/.codex/auth.json && sudo install -o $user -g $user -m 600 /tmp/config.toml /home/$user/.codex/config.toml && rm -f /tmp/auth.json /tmp/config.toml"
  auth_b64="$(base64 < "$auth_file" | tr -d '\n')"
  upsert_secret_key "$secret_key" "$auth_b64"
  echo "Installed auth for $name -> /home/$user/.codex/auth.json and persisted $secret_key to $secret_id" >&2
done
