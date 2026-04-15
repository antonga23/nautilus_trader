#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <worker-name>" >&2
  exit 1
fi

worker_name="$1"
repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
config_path="$repo_root/scripts/symphony/workers.json"

if [ ! -f "$config_path" ]; then
  echo "Missing worker config: $config_path" >&2
  exit 1
fi

if ! jq -e --arg name "$worker_name" '.workers[] | select(.name == $name)' "$config_path" > /dev/null; then
  echo "Unknown worker: $worker_name" >&2
  exit 1
fi

worker_home="$HOME/.codex-workers/$worker_name"
worker_email="$(jq -r --arg name "$worker_name" '.workers[] | select(.name == $name) | .email' "$config_path")"

mkdir -p "$worker_home"
chmod 700 "$worker_home"
cat > "$worker_home/config.toml" << 'CONFIG'
cli_auth_credentials_store = "file"
forced_login_method = "chatgpt"
CONFIG

export CODEX_HOME="$worker_home"

echo "Logging in for $worker_name using isolated CODEX_HOME=$CODEX_HOME" >&2
echo "Expected ChatGPT account: $worker_email" >&2
echo "After login, auth will be stored in $worker_home/auth.json" >&2
codex login --device-auth

if [ ! -f "$worker_home/auth.json" ]; then
  echo "Login completed without creating $worker_home/auth.json" >&2
  exit 1
fi

auth_email="$(
  python3 - "$worker_home/auth.json" << 'PY'
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
)"

if [ -z "$auth_email" ]; then
  echo "Could not determine the signed-in ChatGPT email for $worker_name" >&2
  exit 1
fi

if [ "$auth_email" != "$worker_email" ]; then
  echo "Signed-in account mismatch for $worker_name: expected $worker_email but got $auth_email" >&2
  echo "Delete $worker_home/auth.json and re-run this command with the correct account." >&2
  exit 1
fi

echo "Captured valid auth for $worker_name -> $auth_email" >&2
