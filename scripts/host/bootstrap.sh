#!/usr/bin/env bash
#
# bootstrap.sh — provision a fresh Ubuntu host to run betting-arbitrage trading nodes
# plus the nodeops dashboard, health monitor, and disk-hygiene routine. Provider-
# agnostic: any Ubuntu VM reachable over SSH (EC2, GCP, other) is bootstrapped the
# same way. Run as root (or via sudo) from a checkout of this repo on the host:
#
#   scp -r <repo> host:/opt/cloudbet/cloudbet-market-maker   # or: git clone on the host
#   ssh host 'sudo /opt/cloudbet/cloudbet-market-maker/scripts/host/bootstrap.sh \
#              --env-file /path/to/venue.env --webhook https://hooks.example/xyz'
#
# Idempotent: every step is safe to re-run. Secrets are never committed — the venue
# env file is supplied by the caller (by path or on stdin via `--env-file -`).
#
# Flags:
#   --nodes-root <path>          strategy-node root (default /opt/cloudbet/strategy-nodes)
#   --env-file <path|->          venue env file; `-` reads it from stdin
#   --env-dest <path>            where to install it (default /opt/cloudbet/strategy-node.env)
#   --registry-user <user>       GHCR username (required with --registry-token-file)
#   --registry-token-file <path> GHCR token file for `docker login ghcr.io`
#   --nodeops on|off             install/start nodeops dashboard (default on)
#   --health on|off              install the health monitor + timer (default on)
#   --hygiene on|off             install the disk-hygiene routine + timer (default on)
#   --webhook <url>              alert webhook shared by nodeops + health monitor
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

nodes_root="/opt/cloudbet/strategy-nodes"
env_file=""
env_dest="/opt/cloudbet/strategy-node.env"
registry_user=""
registry_token_file=""
enable_nodeops="on"
enable_health="on"
enable_hygiene="on"
webhook=""

log() { printf '[bootstrap] %s\n' "$*"; }
die() {
  printf '[bootstrap] ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nodes-root)
      nodes_root="$2"
      shift 2
      ;;
    --env-file)
      env_file="$2"
      shift 2
      ;;
    --env-dest)
      env_dest="$2"
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
    --nodeops)
      enable_nodeops="$2"
      shift 2
      ;;
    --health)
      enable_health="$2"
      shift 2
      ;;
    --hygiene)
      enable_hygiene="$2"
      shift 2
      ;;
    --webhook)
      webhook="$2"
      shift 2
      ;;
    -h | --help)
      sed -n '2,/^set -Eeuo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || die "must run as root (use sudo)"

# -- 1. packages: docker engine + python3 ---------------------------------------

ensure_packages() {
  local want=()
  command -v docker > /dev/null 2>&1 || want+=("docker.io")
  command -v python3 > /dev/null 2>&1 || want+=("python3")
  if [[ "${#want[@]}" -eq 0 ]]; then
    log "docker and python3 already present"
  elif command -v apt-get > /dev/null 2>&1; then
    log "installing: ${want[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update -y > /dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${want[@]}" > /dev/null
  else
    die "apt-get not found and missing: ${want[*]} (install them manually)"
  fi
  if command -v systemctl > /dev/null 2>&1; then
    systemctl enable --now docker > /dev/null 2>&1 || true
  fi
}

# -- 2. strategy-node directories -----------------------------------------------

ensure_dirs() {
  install -d -m 0755 /opt/cloudbet
  install -d -m 0755 "$nodes_root" "$nodes_root/archives"
  log "strategy-node root ready at $nodes_root (+archives)"
}

# -- 3. venue env file (never committed; supplied by caller) --------------------

place_env_file() {
  [[ -n "$env_file" ]] || {
    log "no --env-file supplied; deploys will need one at $env_dest"
    return 0
  }
  install -d -m 0755 "$(dirname "$env_dest")"
  if [[ "$env_file" == "-" ]]; then
    log "reading venue env file from stdin -> $env_dest"
    umask 077
    cat > "$env_dest"
    chmod 600 "$env_dest"
  else
    [[ -f "$env_file" ]] || die "env file not found: $env_file"
    install -m 0600 "$env_file" "$env_dest"
    log "installed venue env file -> $env_dest (0600)"
  fi
}

# -- 4. optional GHCR login -----------------------------------------------------

ghcr_login() {
  [[ -n "$registry_token_file" ]] || return 0
  [[ -n "$registry_user" ]] || die "--registry-user is required with --registry-token-file"
  [[ -f "$registry_token_file" ]] || die "registry token file not found: $registry_token_file"
  log "docker login ghcr.io as $registry_user"
  docker login ghcr.io -u "$registry_user" --password-stdin < "$registry_token_file" > /dev/null
}

# -- 5. nodeops dashboard (reuse tools/nodeops/install.sh) -----------------------

install_nodeops() {
  [[ "$enable_nodeops" == "on" ]] || {
    log "skipping nodeops (--nodeops off)"
    return 0
  }
  [[ -f "$REPO_ROOT/tools/nodeops/install.sh" ]] || die "tools/nodeops/install.sh missing under $REPO_ROOT"
  local dropin_dir="/etc/systemd/system/nodeops.service.d"
  install -d -m 0755 "$dropin_dir"
  {
    echo "[Service]"
    echo "Environment=NODEOPS_NODES_ROOT=$nodes_root"
    echo "Environment=NODEOPS_ENV_FILE=$env_dest"
    [[ -n "$webhook" ]] && echo "Environment=NODEOPS_ALERT_WEBHOOK=$webhook"
  } > "$dropin_dir/10-bootstrap.conf"
  log "wrote nodeops drop-in $dropin_dir/10-bootstrap.conf"
  NODEOPS_NODES_ROOT="$nodes_root" bash "$REPO_ROOT/tools/nodeops/install.sh"
}

# -- 6 & 7. health monitor + disk hygiene ---------------------------------------

install_health() {
  [[ "$enable_health" == "on" ]] || {
    log "skipping health monitor (--health off)"
    return 0
  }
  NODEOPS_NODES_ROOT="$nodes_root" NODEOPS_ALERT_WEBHOOK="$webhook" \
    bash "$REPO_ROOT/scripts/host/install-node-host-health.sh"
}

install_hygiene() {
  [[ "$enable_hygiene" == "on" ]] || {
    log "skipping disk hygiene (--hygiene off)"
    return 0
  }
  NODEOPS_NODES_ROOT="$nodes_root" bash "$REPO_ROOT/scripts/host/install-disk-hygiene.sh"
}

ensure_packages
ensure_dirs
place_env_file
ghcr_login
install_nodeops
install_health
install_hygiene

# -- summary --------------------------------------------------------------------

cat << SUMMARY

============================================================
 Host bootstrap complete
============================================================
 repo checkout      : $REPO_ROOT
 strategy-node root : $nodes_root
 venue env file     : $env_dest $([[ -f "$env_dest" ]] && echo '(present)' || echo '(MISSING — supply before deploy)')
 nodeops dashboard  : $([[ "$enable_nodeops" == on ]] && echo 'installed (http://127.0.0.1:8090, loopback)' || echo 'skipped')
 health monitor     : $([[ "$enable_health" == on ]] && echo 'installed (node-host-health.timer, ~2min)' || echo 'skipped')
 disk hygiene       : $([[ "$enable_hygiene" == on ]] && echo 'installed (disk-hygiene.timer, daily)' || echo 'skipped')
 alert webhook      : $([[ -n "$webhook" ]] && echo 'configured' || echo 'unset')

 Deploy a node:
   $REPO_ROOT/scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh \\
     --manifest deploy/strategy_nodes/betting_arbitrage/<manifest>.json \\
     --image <registry>/betting-arbitrage-node:<tag> \\
     --name betting-arbitrage-node-<venue> \\
     --env-file $env_dest --root $nodes_root

 Memory preflight before starting a node (refuses over-subscription):
   $REPO_ROOT/scripts/host/node-host-health.sh preflight --need-mb 3072

 Capacity for this host:
   $REPO_ROOT/scripts/host/node-host-health.sh recommend
============================================================
SUMMARY
