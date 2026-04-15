#!/usr/bin/env bash
set -euo pipefail

exec 8<&0

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
config_path="$repo_root/scripts/symphony/workers.json"
state_root="/srv/symphony/worker-state"
issue_identifier="${SYMPHONY_ISSUE_IDENTIFIER:-$(basename "$PWD")}"
workspace_path="$PWD"
codex_bin="${CODEX_BIN:-$(command -v codex)}"
agent_secret_id="${AGENT_SECRET_ID:-cloudbet-market-maker/credentials}"
worker_profile_secret_key="${WORKER_PROVIDER_PROFILE_SECRET_KEY:-WORKER_PROVIDER_PROFILES_JSON}"
export PATH="/home/ubuntu/.local/bin:/home/ubuntu/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin:$PATH"

runtime_profile_overrides='{}'
runtime_profile_overrides_loaded=0

if [ ! -x "$codex_bin" ]; then
  echo "Codex binary not found: $codex_bin" >&2
  exit 1
fi

write_state() {
  local file="$1"
  local payload="$2"
  local tmp_file
  tmp_file="$(mktemp)"
  if [ -f "$file" ]; then
    jq -s '.[0] * .[1]' "$file" <(printf '%s\n' "$payload") > "$tmp_file"
  else
    printf '%s\n' "$payload" | jq . > "$tmp_file"
  fi
  install -m 664 "$tmp_file" "$file"
  rm -f "$tmp_file"
}

worker_auth_present() {
  local worker_user="$1"
  local worker_auth_file="$2"

  if ! id "$worker_user" > /dev/null 2>&1; then
    return 1
  fi

  sudo -u "$worker_user" test -f "$worker_auth_file"
}

prepare_worker_workspace() {
  local worker_user="$1"
  local worker_group="$2"
  local workspace_dir="$3"

  if [ ! -d "$workspace_dir" ]; then
    return
  fi

  if [ "$(stat -c '%U' "$workspace_dir" 2> /dev/null || printf '')" != "$worker_user" ]; then
    sudo chown -R "$worker_user:$worker_group" "$workspace_dir"
  fi

  sudo chmod -R g+rwX "$workspace_dir"
  sudo find "$workspace_dir" -type d -exec chmod g+s {} +

  sudo -u "$worker_user" -H git config --global --replace-all safe.directory "$workspace_dir" > /dev/null 2>&1 || true

  if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPO:-}" ] && [ -d "$workspace_dir/.git" ]; then
    sudo -u "$worker_user" -H git -C "$workspace_dir" remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git" > /dev/null 2>&1 || true
  fi

  if [ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]; then
    sudo -u "$worker_user" -H git config --global credential.helper '!gh auth git-credential' > /dev/null 2>&1 || true
  fi
}

prepare_worker_cache_dirs() {
  local worker_user="$1"
  local worker_group="$2"
  local worker_home="/home/$worker_user"

  sudo install -d -o "$worker_user" -g "$worker_group" -m 755 \
    "$worker_home/.cache" \
    "$worker_home/.cache/pre-commit" \
    "$worker_home/.cache/go-build" \
    "$worker_home/.cache/uv" \
    "$worker_home/.local/state"
}

clear_busy_state() {
  local worker_name="$1"
  local state_file="$state_root/workers/$worker_name.json"
  local now_epoch
  now_epoch="$(date +%s)"
  write_state "$state_file" "$(jq -n --arg status idle --arg issueIdentifier '' --arg workspacePath '' --arg lastEndedAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg lastError '' --argjson lastEndedAtEpoch "$now_epoch" --argjson cooldownUntilEpoch 0 '{status:$status, issueIdentifier:$issueIdentifier, workspacePath:$workspacePath, lastEndedAt:$lastEndedAt, lastError:$lastError, lastEndedAtEpoch:$lastEndedAtEpoch, cooldownUntilEpoch:$cooldownUntilEpoch}')"
}

load_runtime_profile_overrides() {
  if [ "$runtime_profile_overrides_loaded" -eq 1 ]; then
    return
  fi
  runtime_profile_overrides_loaded=1
  runtime_profile_overrides='{}'

  if ! command -v aws > /dev/null 2>&1; then
    return
  fi

  local secret_json
  if ! secret_json="$(aws secretsmanager get-secret-value --secret-id "$agent_secret_id" --query SecretString --output text 2> /dev/null)"; then
    return
  fi

  runtime_profile_overrides="$(jq -r --arg key "$worker_profile_secret_key" '.[$key] // "{}"' <<< "$secret_json" 2> /dev/null || printf '{}')"
  if ! jq -e . > /dev/null 2>&1 <<< "$runtime_profile_overrides"; then
    runtime_profile_overrides='{}'
  fi
}

get_worker_codex_profile_field() {
  local worker_name="$1"
  local field_name="$2"
  load_runtime_profile_overrides
  jq -r --arg name "$worker_name" --argjson overrides "$runtime_profile_overrides" --arg field "$field_name" '
    (.workers[] | select(.name == $name) | .providerProfiles.codex // {}) as $base
    | ($overrides[$name].codex // {}) as $override
    | ($base + $override) as $profile
    | ($profile[$field] // "")
  ' "$config_path"
}

select_and_run() {
  local worker_json="$1"
  local name user email priority state_file lock_file auth_file state_json cooldown_until cordoned run_log exit_code now_epoch effective_model subscription_tier
  local bridge_bin worker_group

  name="$(jq -r '.name' <<< "$worker_json")"
  user="$(jq -r '.user' <<< "$worker_json")"
  email="$(jq -r '.email' <<< "$worker_json")"
  priority="$(jq -r '.priority // 999' <<< "$worker_json")"
  state_file="$state_root/workers/$name.json"
  lock_file="$state_root/locks/$name.lock"
  auth_file="/home/$user/.codex/auth.json"
  now_epoch="$(date +%s)"

  if ! id "$user" > /dev/null 2>&1; then
    write_state "$state_file" "$(jq -n --arg status unavailable --arg lastError "Missing Linux user $user" '{status:$status,lastError:$lastError}')"
    return 1
  fi

  if ! worker_auth_present "$user" "$auth_file"; then
    write_state "$state_file" "$(jq -n --arg status missing_auth --arg lastError "Missing auth.json for $user" --arg email "$email" '{status:$status,lastError:$lastError,email:$email}')"
    return 1
  fi

  state_json='{}'
  if [ -f "$state_file" ]; then
    state_json="$(cat "$state_file")"
  fi

  cooldown_until="$(jq -r '.cooldownUntilEpoch // 0' <<< "$state_json")"
  cordoned="$(jq -r '.cordoned // false' <<< "$state_json")"

  if [ "$cordoned" = "true" ]; then
    return 1
  fi

  if [ "$cooldown_until" -gt "$now_epoch" ]; then
    write_state "$state_file" "$(jq -n --arg status cooldown --arg email "$email" --argjson cooldownUntilEpoch "$cooldown_until" '{status:$status,email:$email,cooldownUntilEpoch:$cooldownUntilEpoch}')"
    return 1
  fi

  exec 9> "$lock_file"
  if ! flock -n 9; then
    exec 9>&-
    return 1
  fi

  run_log="/var/log/symphony/workers/${name}.stderr.log"
  : > "$run_log"
  effective_model="$(get_worker_codex_profile_field "$name" "runtimeModel")"
  subscription_tier="$(get_worker_codex_profile_field "$name" "subscriptionTier")"
  bridge_bin="$repo_root/scripts/symphony/codex_control_bridge.mjs"
  worker_group="$(id -gn "$user")"

  prepare_worker_workspace "$user" "$worker_group" "$workspace_path"
  prepare_worker_cache_dirs "$user" "$worker_group"

  write_state "$state_file" "$(jq -n \
    --arg status busy \
    --arg name "$name" \
    --arg email "$email" \
    --arg issueIdentifier "$issue_identifier" \
    --arg workspacePath "$workspace_path" \
    --arg startedAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson startedAtEpoch "$now_epoch" \
    --arg workerUser "$user" \
    --arg effectiveModel "$effective_model" \
    --arg subscriptionTier "$subscription_tier" \
    --argjson priority "$priority" \
    '{status:$status,name:$name,email:$email,issueIdentifier:$issueIdentifier,workspacePath:$workspacePath,startedAt:$startedAt,startedAtEpoch:$startedAtEpoch,workerUser:$workerUser,effectiveModel:$effectiveModel,subscriptionTier:$subscriptionTier,priority:$priority}')"

  set +e
  sudo -u "$user" -H env \
    HOME="/home/$user" \
    CODEX_HOME="/home/$user/.codex" \
    XDG_CACHE_HOME="/home/$user/.cache" \
    PRE_COMMIT_HOME="/home/$user/.cache/pre-commit" \
    GOCACHE="/home/$user/.cache/go-build" \
    UV_CACHE_DIR="/home/$user/.cache/uv" \
    PATH="/home/$user/.local/bin:/home/$user/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin" \
    SYMPHONY_WORKER_NAME="$name" \
    SYMPHONY_WORKER_EMAIL="$email" \
    SYMPHONY_ISSUE_IDENTIFIER="$issue_identifier" \
    SYMPHONY_EFFECTIVE_MODEL="$effective_model" \
    CODEX_BIN="$codex_bin" \
    GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
    GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}" \
    GITHUB_REPO="${GITHUB_REPO:-}" \
    /usr/bin/env node "$bridge_bin" \
    <&8 \
    2> >(tee -a "$run_log" >&2)
  exit_code=$?
  set -e

  if grep -qiE 'HTTP 429|rate limit|credits balance|too many requests' "$run_log" 2> /dev/null; then
    write_state "$state_file" "$(jq -n \
      --arg status rate_limited \
      --arg issueIdentifier "$issue_identifier" \
      --arg workspacePath "$workspace_path" \
      --arg lastError "Codex rate limit detected" \
      --arg lastEndedAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --argjson lastEndedAtEpoch "$(date +%s)" \
      --argjson cooldownUntilEpoch "$(($(date +%s) + 3600))" \
      '{status:$status,issueIdentifier:$issueIdentifier,workspacePath:$workspacePath,lastError:$lastError,lastEndedAt:$lastEndedAt,lastEndedAtEpoch:$lastEndedAtEpoch,cooldownUntilEpoch:$cooldownUntilEpoch}')"
  else
    clear_busy_state "$name"
  fi

  exec 9>&-
  exit "$exit_code"
}

mapfile -t workers < <(jq -c '.workers | sort_by(.priority // 999)[] | select(.enabled != false)' "$config_path")
if [ "${#workers[@]}" -eq 0 ]; then
  echo "No enabled workers defined in $config_path" >&2
  exit 75
fi

for worker_json in "${workers[@]}"; do
  if select_and_run "$worker_json"; then
    exit 0
  fi
done

echo "No available authenticated Codex worker for issue $issue_identifier" >&2
exit 75
