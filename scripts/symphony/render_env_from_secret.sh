#!/usr/bin/env bash
set -euo pipefail

secret_id="${1:-cloudbet-market-maker/credentials}"
output_path="${2:-/srv/symphony/symphony.env}"

if ! command -v aws > /dev/null 2>&1; then
  if [ -f "$output_path" ]; then
    echo "aws CLI not available; reusing existing $output_path" >&2
    exit 0
  fi
  echo "aws CLI is required" >&2
  exit 1
fi

if ! command -v jq > /dev/null 2>&1; then
  if [ -f "$output_path" ]; then
    echo "jq not available; reusing existing $output_path" >&2
    exit 0
  fi
  echo "jq is required" >&2
  exit 1
fi

aws_region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$aws_region" ]; then
  aws_region="$(aws configure get region 2> /dev/null || true)"
fi
if [ -z "$aws_region" ]; then
  if [ -f "$output_path" ]; then
    echo "AWS region not configured; reusing existing $output_path" >&2
    exit 0
  fi
  echo "AWS region is required to render $output_path" >&2
  exit 1
fi

if ! aws sts get-caller-identity --output json > /dev/null 2>&1; then
  if [ -f "$output_path" ]; then
    echo "AWS credentials unavailable; reusing existing $output_path" >&2
    exit 0
  fi
  echo "AWS credentials are required to render $output_path" >&2
  exit 1
fi

codex_bin="$(command -v codex || true)"
if [ -z "$codex_bin" ]; then
  codex_bin="codex"
  echo "codex CLI not found; rendering CODEX_BIN=codex for later runtime provisioning" >&2
fi

tmp_file=$(mktemp)
secret_json_file=$(mktemp)
trap 'rm -f "$tmp_file" "$secret_json_file"' EXIT

aws secretsmanager get-secret-value \
  --secret-id "$secret_id" \
  --query SecretString \
  --output text > "$secret_json_file"

python3 - "$tmp_file" "$secret_json_file" << 'PY'
import json
import re
import shlex
import sys

output_path = sys.argv[1]
secret_json_path = sys.argv[2]
with open(secret_json_path, encoding="utf-8") as fh:
    payload = json.load(fh)
valid_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

with open(output_path, "w", encoding="utf-8") as fh:
    for key, value in payload.items():
        if value is None or not valid_name.match(key):
            continue
        fh.write(f"{key}={shlex.quote(str(value))}\n")
PY

github_token="$(jq -r '.GITHUB_TOKEN // ""' "$secret_json_file")"
gh_token="$(jq -r '.GH_TOKEN // ""' "$secret_json_file")"
if [ -n "$github_token" ] && [ -z "$gh_token" ]; then
  printf 'GH_TOKEN=%q\n' "$github_token" >> "$tmp_file"
fi

cat >> "$tmp_file" << 'EOF'
SYMPHONY_WORKSPACE_ROOT=/srv/symphony/workspaces
SOURCE_REPO_URL=/srv/symphony/control-repo
SYMPHONY_PORT=4000
CONTROL_PLANE_PORT=4100
CONTROL_PLANE_WORKER_CONFIG=/srv/symphony/control-repo/scripts/symphony/workers.json
AGENT_SECRET_ID=cloudbet-market-maker/credentials
GCP_SERVICE_ACCOUNT_PATH=/srv/symphony/gcp-service-account.json
GCP_GCLOUD_CONFIG_DIR=/srv/symphony/gcloud-config
CLOUDSDK_CONFIG=/srv/symphony/gcloud-config
GOOGLE_APPLICATION_CREDENTIALS=/srv/symphony/gcp-service-account.json
EOF

printf 'CODEX_BIN=%q\n' "$codex_bin" >> "$tmp_file"

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
