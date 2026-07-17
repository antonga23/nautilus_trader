#!/usr/bin/env bash
# Idempotent installer for the standing semantic-rule miner on the strategy-node
# deploy host.
#
# The miner needs the exact nautilus_trader the trading nodes run; a host pip
# install from the checkout would require a full Rust/Cython build toolchain and
# drift from the deployed image. So the service loop runs INSIDE the
# strategy-node docker image: systemd supervises a foreground `docker run`
# whose mounts use identical host/container paths, keeping every MINER_*
# default valid inside the container.
#
# Config is read at runtime from /opt/cloudbet/miner/miner.env, which you must
# fill in BEFORE first start (chmod 600, root-owned). It is NOT committed:
#
#   MINER_NODE_IMAGE=...     # strategy-node image ref (required; the installer
#                            # pre-fills it from the newest current-image.txt
#                            # under /opt/cloudbet/strategy-nodes when present)
#   SXBET_API_KEY=...        # venue credentials for the corpus refresh phases
#   CLOUDBET_API_KEY=...
#   # Optional overrides (defaults shown):
#   # MINER_MASTER_DIR=/opt/cloudbet/miner/master-cache
#   # MINER_INTERVAL_HOURS=6
#   # MINER_MANIFEST=/opt/cloudbet/miner/mine-manifest.json
#   # MINER_NODES_ROOT=/opt/cloudbet/strategy-nodes
#   # MINER_HOT_SWAP=1
#   # MINER_TEMPLATE_STALE_DAYS=14
#   # MINER_MAX_DISK_GB=10
#   # MINER_LOG_LEVEL=INFO
set -euo pipefail

INSTALL_DIR="/opt/cloudbet/miner"
ENV_FILE="${INSTALL_DIR}/miner.env"
NODES_ROOT="/opt/cloudbet/strategy-nodes"
UNIT_SRC_NAME="miner.service"
UNIT_DEST="/etc/systemd/system/miner.service"
SERVICE_NAME="miner"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install.sh must run as root (sudo)." >&2
  exit 1
fi

echo "Installing miner into ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

install -m 0644 "${SCRIPT_DIR}/miner_service.py" "${INSTALL_DIR}/miner_service.py"
install -m 0644 "${SCRIPT_DIR}/mine-manifest.json" "${INSTALL_DIR}/mine-manifest.json"

default_image=""
if [[ -d "${NODES_ROOT}" ]]; then
  newest_image_file="$(find "${NODES_ROOT}" -mindepth 2 -maxdepth 2 -type f \
    -name current-image.txt -printf '%T@ %p\n' 2> /dev/null |
    sort -rn | head -n 1 | cut -d' ' -f2- || true)"
  if [[ -n "${newest_image_file}" && -f "${newest_image_file}" ]]; then
    default_image="$(head -n 1 "${newest_image_file}" | tr -d '[:space:]')"
  fi
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "WARNING: ${ENV_FILE} did not exist. Fill in MINER_NODE_IMAGE and the venue"
  echo "         API keys before the service can mine. See the header of this script."
  install -m 0600 /dev/null "${ENV_FILE}"
  {
    echo "# Fill in MINER_NODE_IMAGE / SXBET_API_KEY / CLOUDBET_API_KEY — see install.sh header."
    if [[ -n "${default_image}" ]]; then
      echo "MINER_NODE_IMAGE=${default_image}"
    else
      echo "# MINER_NODE_IMAGE=<strategy-node image ref>"
    fi
  } > "${ENV_FILE}"
fi
chmod 600 "${ENV_FILE}"

if [[ -f "${UNIT_DEST}" ]]; then
  echo "Existing ${UNIT_DEST} kept; refreshing code only."
else
  echo "Installing systemd unit ${UNIT_DEST}"
  install -m 0644 "${SCRIPT_DIR}/${UNIT_SRC_NAME}" "${UNIT_DEST}"
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo
echo "miner installed. It reads ${ENV_FILE} for the image ref + credentials."
echo "  * Check status:  systemctl status ${SERVICE_NAME}"
echo "  * Follow logs:   journalctl -u ${SERVICE_NAME} -f"
echo "  * The miner is additive: the master cache accumulates and is never reset."
