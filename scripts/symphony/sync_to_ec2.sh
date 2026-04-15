#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"

cd "$repo_root"
set -a
# shellcheck source=/dev/null
source .env
set +a

chmod 600 "$EC2_KEY_PATH"

ssh_opts=(
  -i "$EC2_KEY_PATH"
  -o StrictHostKeyChecking=no
)

ssh "${ssh_opts[@]}" "$EC2_USER@$EC2_HOST" '
  set -euo pipefail
  mkdir -p /srv/symphony/control-repo
'

paths=(
  scripts/symphony
)

for optional_path in WORKFLOW.md .codex .agents .github/pull_request_template.md; do
  if [ -e "$optional_path" ]; then
    paths+=("$optional_path")
  fi
done

COPYFILE_DISABLE=1 tar czf - \
  --exclude='._*' \
  --exclude='.DS_Store' \
  --exclude='scripts/symphony/control_plane/node_modules' \
  --exclude='scripts/symphony/control_plane/dist' \
  --exclude='scripts/symphony/control_plane/.vite' \
  "${paths[@]}" |
  ssh "${ssh_opts[@]}" "$EC2_USER@$EC2_HOST" '
      set -euo pipefail
      tar xzf - -C /srv/symphony/control-repo
    '

ssh "${ssh_opts[@]}" "$EC2_USER@$EC2_HOST" '
  set -euo pipefail
  find /srv/symphony/control-repo/scripts/symphony -type f -name "*.sh" -exec chmod +x {} +
'
