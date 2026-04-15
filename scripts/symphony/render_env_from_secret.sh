#!/usr/bin/env bash
set -euo pipefail

secret_id="${1:-cloudbet-market-maker/credentials}"
output_path="${2:-/srv/symphony/symphony.env}"

if ! command -v aws > /dev/null 2>&1; then
  echo "aws CLI is required" >&2
  exit 1
fi

if ! command -v jq > /dev/null 2>&1; then
  echo "jq is required" >&2
  exit 1
fi

codex_bin="$(command -v codex || true)"
if [ -z "$codex_bin" ]; then
  codex_bin="codex"
  echo "codex CLI not found; rendering CODEX_BIN=codex for later runtime provisioning" >&2
fi

secret_json=$(
  aws secretsmanager get-secret-value \
    --secret-id "$secret_id" \
    --query SecretString \
    --output text
)

tmp_file=$(mktemp)
trap 'rm -f "$tmp_file"' EXIT

jq -r '
  to_entries[]
  | select(.value != null)
  | "\(.key)=\(.value)"
' <<< "$secret_json" > "$tmp_file"

github_token="$(jq -r '.GITHUB_TOKEN // ""' <<< "$secret_json")"
gh_token="$(jq -r '.GH_TOKEN // ""' <<< "$secret_json")"
if [ -n "$github_token" ] && [ -z "$gh_token" ]; then
  printf 'GH_TOKEN=%s\n' "$github_token" >> "$tmp_file"
fi

cat >> "$tmp_file" << 'EOF'
SYMPHONY_WORKSPACE_ROOT=/srv/symphony/workspaces
SOURCE_REPO_URL=/srv/symphony/control-repo
SYMPHONY_PORT=4000
CONTROL_PLANE_PORT=4100
CONTROL_PLANE_WORKER_CONFIG=/srv/symphony/control-repo/scripts/symphony/workers.json
AGENT_SECRET_ID=cloudbet-market-maker/credentials
EOF

printf 'CODEX_BIN=%s\n' "$codex_bin" >> "$tmp_file"

output_dir="$(dirname "$output_path")"
install -d "$output_dir"
if getent group symphony > /dev/null 2>&1; then
  chgrp symphony "$output_dir" 2> /dev/null || true
  chmod 2775 "$output_dir" 2> /dev/null || true
else
  chmod 755 "$output_dir" 2> /dev/null || true
fi
if ! install -m 600 "$tmp_file" "$output_path" 2> /dev/null; then
  sudo install -m 600 -o "$(id -un)" -g "$(id -gn)" "$tmp_file" "$output_path"
fi
