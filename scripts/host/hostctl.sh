#!/usr/bin/env bash
#
# hostctl.sh — operator CLI for registry-driven multi-host trading-node operations.
# Runs on the operator's machine and drives any SSH-reachable host registered in
# deploy/hosts.yaml (or hosts.json) — no GitHub runner is needed on the target.
# Everything is shipped per invocation from the local checkout, so the target host
# needs neither a repo clone nor GHCR credentials.
#
# Subcommands:
#   list                       Print the host registry.
#   bootstrap <host>           Ship the host toolkit + nodeops and run bootstrap.sh
#                              (sudo) with --nodes-root from the registry.
#     --env-file <path>          venue env file, streamed over stdin (never staged)
#     --webhook <url>            alert webhook for nodeops + health monitor
#   deploy-node <host>         Deploy/redeploy a trading node on the host. Runs the
#                              memory preflight first and REFUSES on failure.
#     --manifest <path>          local node manifest JSON (required)
#     --image <ref>              image reference the container runs (required)
#     --name <container>         container name (required)
#     --image-archive <path>     local docker-save archive (.tar/.tar.gz) to ship
#     --env-file <path>          local venue env file, shipped 0600 and removed after
#     --host-env-file <path>     env file already on the host (e.g. the bootstrap-
#                                installed /opt/cloudbet/strategy-node.env)
#     --need-mb <int>            preflight memory requirement (default 3072)
#     --transport <mode>         auto|pull|save|archive (default auto; see below)
#     --registry-user <user>     GHCR username for pull transport
#     --registry-token-file <p>  local GHCR token file, shipped 0600 and removed after
#   deploy-nodeops <host>      Install/update the nodeops dashboard on the host.
#     --webhook <url>            alert webhook written into the systemd drop-in
#     --env-dest <path>          venue env file path on the host (default
#                                /opt/cloudbet/strategy-node.env)
#   status <host>              Read-only SSH snapshot: uptime, disk, MemAvailable,
#                              betting containers + heartbeat/status file ages.
#   self-test                  Registry-parser + preflight-gate + dry-run assertions
#                              (no SSH, no host access).
#
# Common flags (any subcommand):
#   --registry <file>          registry file (default: deploy/hosts.json, then
#                              deploy/hosts.yaml, then hosts.example.yaml + warning)
#   --identity | -i <keyfile>  SSH identity (default: registry identity_file_hint
#                              when it resolves to a local file)
#   --dry-run                  print the exact ssh/scp/docker commands, run nothing
#
# Image transport (deploy-node): `pull` lets the host pull the ref (optionally after
# a GHCR login with --registry-user/--registry-token-file); `save` streams a local
# `docker save` of the ref over SSH for hosts without registry credentials;
# `archive` ships a pre-saved --image-archive (e.g. the release workflow's
# betting-arbitrage-node.tar.gz) and `docker load`s it. `auto` picks: archive when
# --image-archive is given, else pull when a token file is given, else save when the
# ref exists in the local docker daemon, else pull.
#
# Secrets stay operator-side: env files and registry tokens are never stored in the
# registry and only ever shipped per-deploy (0600, removed with the remote stage).
# shellcheck disable=SC2029 # remote commands are composed locally on purpose: every
# stage path / flag is expanded operator-side so --dry-run prints the literal command.
set -Eeuo pipefail
trap 'hc_log error "hostctl failed at line ${LINENO}: ${BASH_COMMAND}"' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/host/host_common.sh
source "$SCRIPT_DIR/host_common.sh"

DEFAULT_NEED_MB=3072
DEFAULT_ENV_DEST="/opt/cloudbet/strategy-node.env"

DRY_RUN=0
IDENTITY=""
REGISTRY_FILE=""

reg_name=""
reg_ssh_host=""
reg_ssh_user=""
reg_identity_hint=""
reg_nodes_root=""
reg_nodeops_url=""
SSH_ARGS=()
REMOTE_STAGE=""

usage() {
  sed -n '2,/^# shellcheck disable/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; s/^#//; $d'
}

die() {
  hc_log error "$*"
  exit 1
}

# -- registry -----------------------------------------------------------------

resolve_registry() {
  if [[ -n "$REGISTRY_FILE" ]]; then
    if [[ ! -f "$REGISTRY_FILE" ]]; then die "registry not found: $REGISTRY_FILE"; fi
    return 0
  fi
  local candidate
  for candidate in "$REPO_ROOT/deploy/hosts.json" "$REPO_ROOT/deploy/hosts.yaml"; do
    if [[ -f "$candidate" ]]; then
      REGISTRY_FILE="$candidate"
      return 0
    fi
  done
  REGISTRY_FILE="$REPO_ROOT/deploy/hosts.example.yaml"
  hc_log warn "no deploy/hosts.json or deploy/hosts.yaml; falling back to the committed example (documentation hosts only)"
}

load_host() {
  local name="$1" line key value
  resolve_registry
  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      name) reg_name="$value" ;;
      ssh_host) reg_ssh_host="$value" ;;
      ssh_user) reg_ssh_user="$value" ;;
      identity_file_hint) reg_identity_hint="$value" ;;
      nodes_root) reg_nodes_root="$value" ;;
      nodeops_url) reg_nodeops_url="$value" ;;
    esac
  done < <(python3 "$SCRIPT_DIR/registry.py" get "$REGISTRY_FILE" "$name")
  if [[ -z "$reg_name" || -z "$reg_ssh_host" || -z "$reg_ssh_user" ]]; then
    die "could not load host '$name' from $REGISTRY_FILE"
  fi
  build_ssh_args
  hc_log info "target $reg_name = $reg_ssh_user@$reg_ssh_host (registry $REGISTRY_FILE)"
}

build_ssh_args() {
  SSH_ARGS=(-o BatchMode=yes -o ConnectTimeout=10)
  local identity="$IDENTITY"
  if [[ -z "$identity" && -n "$reg_identity_hint" ]]; then
    local expanded="${reg_identity_hint/#\~/$HOME}"
    if [[ -f "$expanded" ]]; then
      identity="$expanded"
    else
      hc_log info "identity_file_hint '$reg_identity_hint' is not a local file; relying on ssh config/agent"
    fi
  fi
  if [[ -n "$identity" ]]; then
    SSH_ARGS+=(-i "$identity")
  fi
}

# -- command runners (every remote action goes through these; --dry-run prints) --

ssh_target() {
  printf '%s@%s' "$reg_ssh_user" "$reg_ssh_host"
}

run_ssh() {
  # run_ssh <remote command string>
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN: ssh %s %s -- %s\n' "${SSH_ARGS[*]}" "$(ssh_target)" "$1"
    return 0
  fi
  ssh "${SSH_ARGS[@]}" "$(ssh_target)" "$1"
}

run_ssh_stdin() {
  # run_ssh_stdin <local input file|-> <remote command string>
  local input="$1" remote_cmd="$2"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN: ssh %s %s -- %s < %s\n' "${SSH_ARGS[*]}" "$(ssh_target)" "$remote_cmd" "$input"
    return 0
  fi
  if [[ "$input" == "-" ]]; then
    ssh "${SSH_ARGS[@]}" "$(ssh_target)" "$remote_cmd"
  else
    ssh "${SSH_ARGS[@]}" "$(ssh_target)" "$remote_cmd" < "$input"
  fi
}

open_remote_stage() {
  if [[ "$DRY_RUN" == "1" ]]; then
    REMOTE_STAGE="<remote-stage>"
    printf 'DRY-RUN: ssh %s %s -- mktemp -d /tmp/hostctl.XXXXXX\n' "${SSH_ARGS[*]}" "$(ssh_target)"
    return 0
  fi
  REMOTE_STAGE="$(ssh "${SSH_ARGS[@]}" "$(ssh_target)" "mktemp -d /tmp/hostctl.XXXXXX")"
  if [[ -z "$REMOTE_STAGE" ]]; then die "failed to create remote stage directory"; fi
  # Best-effort cleanup even when a later step fails; secrets in the stage are 0600.
  trap 'cleanup_remote_stage' EXIT
}

cleanup_remote_stage() {
  if [[ -n "$REMOTE_STAGE" && "$REMOTE_STAGE" != "<remote-stage>" ]]; then
    ssh "${SSH_ARGS[@]}" "$(ssh_target)" "rm -rf '$REMOTE_STAGE'" > /dev/null 2>&1 || true
    REMOTE_STAGE=""
  fi
}

stage_repo_paths() {
  # stage_repo_paths <repo-relative path...> — stream paths into the remote stage,
  # preserving layout so bootstrap.sh's REPO_ROOT-relative lookups keep working.
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN: tar -C %s -czf - %s | ssh %s %s -- tar -xzf - -C %s\n' \
      "$REPO_ROOT" "$*" "${SSH_ARGS[*]}" "$(ssh_target)" "$REMOTE_STAGE"
    return 0
  fi
  tar -C "$REPO_ROOT" -czf - "$@" |
    ssh "${SSH_ARGS[@]}" "$(ssh_target)" "tar -xzf - -C '$REMOTE_STAGE'"
}

ship_file() {
  # ship_file <local path> <remote path> [mode] — stream one file (no scp; a single
  # ssh code path keeps --dry-run and identity handling uniform). mode 0600 for
  # secrets (written under umask 077).
  local local_path="$1" remote_path="$2" mode="${3:-0644}"
  if [[ ! -f "$local_path" ]]; then die "local file not found: $local_path"; fi
  local remote_cmd="cat > '$remote_path' && chmod $mode '$remote_path'"
  if [[ "$mode" == "0600" ]]; then
    remote_cmd="umask 077 && $remote_cmd"
  fi
  run_ssh_stdin "$local_path" "$remote_cmd"
}

# -- memory preflight gate (the anti-OOM guard, wired into every deploy-node) ----

remote_preflight() {
  # Runs the Phase-1 preflight with the copy just staged on the host, so it works
  # even on hosts that have not installed the health monitor yet.
  local need_mb="$1"
  run_ssh "bash '$REMOTE_STAGE/scripts/host/node-host-health.sh' preflight --need-mb $need_mb"
}

preflight_gate() {
  local need_mb="$1"
  if [[ "$DRY_RUN" == "1" ]]; then
    remote_preflight "$need_mb"
    hc_log info "dry-run: deploy proceeds only if the preflight above exits 0"
    return 0
  fi
  if remote_preflight "$need_mb"; then
    hc_log info "memory preflight passed on $reg_name (need ${need_mb}MB)"
    return 0
  fi
  hc_log error "REFUSING deploy: memory preflight failed on $reg_name (need ${need_mb}MB) — starting this node could OOM the host"
  return 1
}

# -- subcommands ----------------------------------------------------------------

cmd_list() {
  resolve_registry
  python3 "$SCRIPT_DIR/registry.py" list "$REGISTRY_FILE"
}

cmd_bootstrap() {
  local host="" env_file="" webhook=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --env-file)
        env_file="$2"
        shift 2
        ;;
      --webhook)
        webhook="$2"
        shift 2
        ;;
      -*)
        die "bootstrap: unknown flag $1"
        ;;
      *)
        host="$1"
        shift
        ;;
    esac
  done
  if [[ -z "$host" ]]; then die "bootstrap: host name required"; fi
  if [[ -n "$env_file" && ! -f "$env_file" ]]; then die "env file not found: $env_file"; fi
  load_host "$host"
  open_remote_stage

  stage_repo_paths scripts/host tools/nodeops scripts/deploy/strategy_nodes

  local remote_cmd="sudo bash '$REMOTE_STAGE/scripts/host/bootstrap.sh' --nodes-root '$reg_nodes_root'"
  if [[ -n "$webhook" ]]; then
    remote_cmd+=" --webhook '$webhook'"
  fi
  if [[ -n "$env_file" ]]; then
    # Stream the venue env file over stdin so it never lands in the stage dir.
    run_ssh_stdin "$env_file" "$remote_cmd --env-file -"
  else
    hc_log warn "no --env-file: deploys will need one at $DEFAULT_ENV_DEST"
    run_ssh "$remote_cmd"
  fi

  run_ssh "rm -rf '$REMOTE_STAGE'"
  REMOTE_STAGE=""
  hc_log info "bootstrap of $reg_name complete"
}

cmd_deploy_node() {
  local host="" manifest="" image="" name="" image_archive="" env_file="" host_env_file=""
  local need_mb="$DEFAULT_NEED_MB" transport="auto" registry_user="" registry_token_file=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --manifest)
        manifest="$2"
        shift 2
        ;;
      --image)
        image="$2"
        shift 2
        ;;
      --name)
        name="$2"
        shift 2
        ;;
      --image-archive)
        image_archive="$2"
        shift 2
        ;;
      --env-file)
        env_file="$2"
        shift 2
        ;;
      --host-env-file)
        host_env_file="$2"
        shift 2
        ;;
      --need-mb)
        need_mb="$2"
        shift 2
        ;;
      --transport)
        transport="$2"
        shift 2
        ;;
      --registry-user)
        registry_user="$2"
        shift 2
        ;;
      --registry-token-file)
        registry_token_file="$2"
        shift 2
        ;;
      -*)
        die "deploy-node: unknown flag $1"
        ;;
      *)
        host="$1"
        shift
        ;;
    esac
  done
  if [[ -z "$host" || -z "$manifest" || -z "$image" || -z "$name" ]]; then
    die "deploy-node requires <host> --manifest <path> --image <ref> --name <container>"
  fi
  if [[ ! -f "$manifest" ]]; then die "manifest not found: $manifest"; fi
  if ! hc_is_uint "$need_mb"; then die "--need-mb must be a positive integer"; fi
  if [[ -n "$env_file" && -n "$host_env_file" ]]; then
    die "use either --env-file (shipped) or --host-env-file (already on host), not both"
  fi
  if [[ -n "$image_archive" && ! -f "$image_archive" ]]; then
    die "image archive not found: $image_archive"
  fi
  if [[ -n "$registry_token_file" ]]; then
    if [[ ! -f "$registry_token_file" ]]; then die "registry token file not found: $registry_token_file"; fi
    if [[ -z "$registry_user" ]]; then die "--registry-user is required with --registry-token-file"; fi
  fi

  case "$transport" in
    auto)
      if [[ -n "$image_archive" ]]; then
        transport="archive"
      elif [[ -n "$registry_token_file" ]]; then
        transport="pull"
      elif docker image inspect "$image" > /dev/null 2>&1; then
        transport="save"
      else
        transport="pull"
      fi
      ;;
    pull | save) ;;
    archive)
      if [[ -z "$image_archive" ]]; then die "--transport archive requires --image-archive"; fi
      ;;
    *)
      die "--transport must be auto|pull|save|archive"
      ;;
  esac
  if [[ "$transport" == "save" && "$DRY_RUN" != "1" ]]; then
    if ! docker image inspect "$image" > /dev/null 2>&1; then
      die "transport 'save' needs image '$image' in the local docker daemon (docker pull it first, or use --image-archive / --registry-token-file)"
    fi
  fi
  hc_log info "image transport: $transport"

  load_host "$host"
  open_remote_stage
  stage_repo_paths scripts/host scripts/deploy/strategy_nodes
  ship_file "$manifest" "$REMOTE_STAGE/manifest.json"

  # The anti-OOM guard: refuse before any image bytes move or containers change.
  preflight_gate "$need_mb"

  case "$transport" in
    archive)
      ship_file "$image_archive" "$REMOTE_STAGE/image-archive"
      run_ssh "if ! docker load -i '$REMOTE_STAGE/image-archive'; then sudo docker load -i '$REMOTE_STAGE/image-archive'; fi && rm -f '$REMOTE_STAGE/image-archive'"
      ;;
    save)
      if [[ "$DRY_RUN" == "1" ]]; then
        printf 'DRY-RUN: docker save %s | gzip -1 | ssh %s %s -- %s\n' \
          "$image" "${SSH_ARGS[*]}" "$(ssh_target)" \
          "cat > '$REMOTE_STAGE/image-archive' && { docker load -i '$REMOTE_STAGE/image-archive' || sudo docker load -i '$REMOTE_STAGE/image-archive'; } && rm -f '$REMOTE_STAGE/image-archive'"
      else
        docker save "$image" | gzip -1 |
          ssh "${SSH_ARGS[@]}" "$(ssh_target)" \
            "cat > '$REMOTE_STAGE/image-archive' && { docker load -i '$REMOTE_STAGE/image-archive' || sudo docker load -i '$REMOTE_STAGE/image-archive'; } && rm -f '$REMOTE_STAGE/image-archive'"
      fi
      ;;
    pull) ;; # deploy_betting_strategy_node.sh pulls (after GHCR login when creds shipped)
  esac

  local deploy_cmd="sudo bash '$REMOTE_STAGE/scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh'"
  deploy_cmd+=" --manifest '$REMOTE_STAGE/manifest.json' --image '$image' --name '$name' --root '$reg_nodes_root'"
  if [[ -n "$env_file" ]]; then
    ship_file "$env_file" "$REMOTE_STAGE/node.env" 0600
    deploy_cmd+=" --env-file '$REMOTE_STAGE/node.env'"
  elif [[ -n "$host_env_file" ]]; then
    deploy_cmd+=" --env-file '$host_env_file'"
  else
    hc_log warn "no env file: the node starts without venue credentials (pass --env-file or --host-env-file, e.g. $DEFAULT_ENV_DEST)"
  fi
  if [[ -n "$registry_token_file" ]]; then
    ship_file "$registry_token_file" "$REMOTE_STAGE/registry-token" 0600
    deploy_cmd+=" --registry-user '$registry_user' --registry-token-file '$REMOTE_STAGE/registry-token'"
  fi

  run_ssh "$deploy_cmd"
  run_ssh "rm -rf '$REMOTE_STAGE'"
  REMOTE_STAGE=""
  hc_log info "deploy of $name to $reg_name complete"
}

cmd_deploy_nodeops() {
  local host="" webhook="" env_dest="$DEFAULT_ENV_DEST"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --webhook)
        webhook="$2"
        shift 2
        ;;
      --env-dest)
        env_dest="$2"
        shift 2
        ;;
      -*)
        die "deploy-nodeops: unknown flag $1"
        ;;
      *)
        host="$1"
        shift
        ;;
    esac
  done
  if [[ -z "$host" ]]; then die "deploy-nodeops: host name required"; fi
  load_host "$host"
  open_remote_stage
  stage_repo_paths tools/nodeops

  # Same drop-in bootstrap.sh writes, so re-running hostctl refreshes those values.
  local dropin_cmd="sudo install -d -m 0755 /etc/systemd/system/nodeops.service.d"
  dropin_cmd+=" && { echo '[Service]'; echo 'Environment=NODEOPS_NODES_ROOT=$reg_nodes_root'; echo 'Environment=NODEOPS_ENV_FILE=$env_dest';"
  if [[ -n "$webhook" ]]; then
    dropin_cmd+=" echo 'Environment=NODEOPS_ALERT_WEBHOOK=$webhook';"
  fi
  dropin_cmd+=" } | sudo tee /etc/systemd/system/nodeops.service.d/10-bootstrap.conf > /dev/null"

  run_ssh "$dropin_cmd && sudo NODEOPS_NODES_ROOT='$reg_nodes_root' bash '$REMOTE_STAGE/tools/nodeops/install.sh'"
  run_ssh "rm -rf '$REMOTE_STAGE'"
  REMOTE_STAGE=""
  hc_log info "nodeops deploy to $reg_name complete (dashboard: ${reg_nodeops_url:-http://127.0.0.1:8090})"
}

cmd_status() {
  local host=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -*)
        die "status: unknown flag $1"
        ;;
      *)
        host="$1"
        shift
        ;;
    esac
  done
  if [[ -z "$host" ]]; then die "status: host name required"; fi
  load_host "$host"

  # Read-only probe; docker falls back to sudo -n and degrades to a note when
  # neither works. Heartbeat/status ages use file mtimes (both files are rewritten
  # on every beat/update) so the probe needs no JSON parsing on the host.
  local probe
  probe="$(
    cat << 'PROBE'
set -u
echo "host:          $(hostname)"
echo "uptime:       $(uptime)"
echo "disk /:        $(df -P / | awk 'NR == 2 {print $5 " used (" $4 " KB free)"}')"
awk '/^MemAvailable:/ {printf "mem available: %d MB\n", $2 / 1024}' /proc/meminfo 2> /dev/null || echo "mem available: unreadable"
echo "containers (${NODE_PREFIX}*):"
docker ps --filter "name=${NODE_PREFIX}" --format '  {{.Names}}\t{{.Status}}' 2> /dev/null ||
  sudo -n docker ps --filter "name=${NODE_PREFIX}" --format '  {{.Names}}\t{{.Status}}' 2> /dev/null ||
  echo "  (docker unavailable to this user)"
echo "node files under ${NODES_ROOT}:"
now="$(date +%s)"
found=0
for f in "${NODES_ROOT}"/*/heartbeat.json "${NODES_ROOT}"/*/status.json; do
  if [ -f "$f" ]; then
    found=1
    mtime="$(stat -c %Y "$f" 2> /dev/null || stat -f %m "$f")"
    echo "  $f: $((now - mtime))s old"
  fi
done
if [ "$found" -eq 0 ]; then echo "  (none)"; fi
PROBE
  )"
  run_ssh "NODES_ROOT='$reg_nodes_root' NODE_PREFIX='${NODE_HOST_NODE_PREFIX:-betting-arbitrage-node}' bash -s << 'HOSTCTL_PROBE'
$probe
HOSTCTL_PROBE"
}

# -- self-test (no SSH, no host access) -------------------------------------------

cmd_self_test() {
  local failures=0
  assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
      printf 'ok   %s\n' "$label"
    else
      printf 'FAIL %s (expected=%s actual=%s)\n' "$label" "$expected" "$actual"
      failures=$((failures + 1))
    fi
  }
  assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
      printf 'ok   %s\n' "$label"
    else
      printf 'FAIL %s (missing %q)\n' "$label" "$needle"
      failures=$((failures + 1))
    fi
  }

  local example="$REPO_ROOT/deploy/hosts.example.yaml"

  # 1. registry parser against the committed example (+ JSON round-trip inside).
  if python3 "$SCRIPT_DIR/registry.py" self-test "$example"; then
    printf 'ok   hostctl/registry_self_test\n'
  else
    printf 'FAIL hostctl/registry_self_test\n'
    failures=$((failures + 1))
  fi

  # 2. load_host wiring: registry fields land in the reg_* variables.
  REGISTRY_FILE="$example"
  load_host betting-dev-ec2 > /dev/null 2>&1
  assert_eq "hostctl/load_host_user" "ubuntu" "$reg_ssh_user"
  assert_eq "hostctl/load_host_host" "203.0.113.10" "$reg_ssh_host"
  assert_eq "hostctl/load_host_nodes_root" "/opt/cloudbet/strategy-nodes" "$reg_nodes_root"

  # 3. preflight gate with injected results: a failing remote preflight must refuse.
  local rc
  remote_preflight() { return 1; }
  rc=0
  preflight_gate 3072 > /dev/null 2>&1 || rc=$?
  assert_eq "hostctl/preflight_gate_refuses" 1 "$rc"
  remote_preflight() { return 0; }
  rc=0
  preflight_gate 3072 > /dev/null 2>&1 || rc=$?
  assert_eq "hostctl/preflight_gate_allows" 0 "$rc"

  # 4. dry-run command plans per subcommand, against the example registry only.
  local out manifest
  manifest="$(mktemp)"
  printf '{}\n' > "$manifest"

  out="$("$SCRIPT_DIR/hostctl.sh" list --registry "$example")"
  assert_contains "dryrun/list_names" "betting-dev-ec2" "$out"

  out="$("$SCRIPT_DIR/hostctl.sh" bootstrap betting-dev-ec2 --registry "$example" --dry-run 2>&1)"
  assert_contains "dryrun/bootstrap_script" "scripts/host/bootstrap.sh" "$out"
  assert_contains "dryrun/bootstrap_nodes_root" "--nodes-root" "$out"
  assert_contains "dryrun/bootstrap_target" "ubuntu@203.0.113.10" "$out"

  out="$("$SCRIPT_DIR/hostctl.sh" deploy-node betting-dev-ec2 --registry "$example" --dry-run \
    --manifest "$manifest" --image ghcr.io/example/betting-arbitrage-node:test \
    --name betting-arbitrage-node-test --transport pull 2>&1)"
  assert_contains "dryrun/deploy_preflight" "preflight --need-mb 3072" "$out"
  assert_contains "dryrun/deploy_script" "deploy_betting_strategy_node.sh" "$out"
  assert_contains "dryrun/deploy_root_flag" "--root '/opt/cloudbet/strategy-nodes'" "$out"

  out="$("$SCRIPT_DIR/hostctl.sh" deploy-node betting-dev-ec2 --registry "$example" --dry-run \
    --manifest "$manifest" --image ghcr.io/example/betting-arbitrage-node:test \
    --name betting-arbitrage-node-test --transport save --need-mb 4096 2>&1)"
  assert_contains "dryrun/deploy_save_stream" "docker save" "$out"
  assert_contains "dryrun/deploy_need_mb_override" "preflight --need-mb 4096" "$out"

  out="$("$SCRIPT_DIR/hostctl.sh" deploy-nodeops betting-dev-ec2 --registry "$example" --dry-run 2>&1)"
  assert_contains "dryrun/nodeops_install" "tools/nodeops/install.sh" "$out"
  assert_contains "dryrun/nodeops_dropin" "NODEOPS_NODES_ROOT=/opt/cloudbet/strategy-nodes" "$out"

  out="$("$SCRIPT_DIR/hostctl.sh" status betting-dev-ec2 --registry "$example" --dry-run 2>&1)"
  assert_contains "dryrun/status_probe" "MemAvailable" "$out"

  rc=0
  "$SCRIPT_DIR/hostctl.sh" deploy-node betting-dev-ec2 --registry "$example" --dry-run \
    --manifest "$manifest" --image example:test > /dev/null 2>&1 || rc=$?
  assert_eq "dryrun/deploy_missing_name_fails" 1 "$rc"

  rm -f "$manifest"
  printf '\nhostctl self-test: %s failure(s)\n' "$failures"
  [[ "$failures" -eq 0 ]]
}

# -- entrypoint -------------------------------------------------------------------

main() {
  if [[ $# -eq 0 ]]; then
    usage
    exit 2
  fi
  local cmd="$1"
  shift

  # Extract common flags; everything else is passed to the subcommand untouched.
  local passthrough=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --registry)
        REGISTRY_FILE="$2"
        shift 2
        ;;
      --identity | -i)
        IDENTITY="$2"
        shift 2
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      *)
        passthrough+=("$1")
        shift
        ;;
    esac
  done
  if [[ "${#passthrough[@]}" -gt 0 ]]; then
    set -- "${passthrough[@]}"
  else
    set --
  fi

  if ! command -v python3 > /dev/null 2>&1; then
    die "python3 is required (registry parsing)"
  fi

  case "$cmd" in
    list)
      cmd_list "$@"
      ;;
    bootstrap)
      cmd_bootstrap "$@"
      ;;
    deploy-node)
      cmd_deploy_node "$@"
      ;;
    deploy-nodeops)
      cmd_deploy_nodeops "$@"
      ;;
    status)
      cmd_status "$@"
      ;;
    self-test | --self-test)
      cmd_self_test
      ;;
    -h | --help | help)
      usage
      ;;
    *)
      hc_log error "unknown subcommand: $cmd"
      usage
      exit 2
      ;;
  esac
}

main "$@"
