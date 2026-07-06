#!/usr/bin/env bash
# Idempotent installer for the nodeops dashboard on the strategy-node deploy host.
#
# Copies server.py + index.html into /opt/cloudbet/nodeops, creates the SQLite db
# directory, installs the systemd unit only if absent (existing NODEOPS_* overrides
# are preserved — re-running just refreshes the code and restarts), then enables
# and starts the service. Re-runnable safely.
set -euo pipefail

INSTALL_DIR="/opt/cloudbet/nodeops"
DB_DIR="/opt/cloudbet/nodeops"
UNIT_SRC_NAME="nodeops.service"
UNIT_DEST="/etc/systemd/system/nodeops.service"
SERVICE_NAME="nodeops"
PORT="${NODEOPS_PORT:-8090}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install.sh must run as root (sudo)." >&2
  exit 1
fi

echo "Installing nodeops into ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
mkdir -p "${DB_DIR}"

install -m 0644 "${SCRIPT_DIR}/server.py" "${INSTALL_DIR}/server.py"
install -m 0644 "${SCRIPT_DIR}/index.html" "${INSTALL_DIR}/index.html"

if [[ -f "${UNIT_DEST}" ]]; then
  echo "Existing ${UNIT_DEST} kept (preserving NODEOPS_* overrides); refreshing code only."
else
  echo "Installing systemd unit ${UNIT_DEST}"
  install -m 0644 "${SCRIPT_DIR}/${UNIT_SRC_NAME}" "${UNIT_DEST}"
fi

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo
echo "nodeops installed (binds 127.0.0.1:${PORT} by default). Dashboard: http://127.0.0.1:${PORT}"
echo "Reminders:"
echo "  * To expose beyond localhost: set NODEOPS_HOST=0.0.0.0 AND NODEOPS_USER/"
echo "    NODEOPS_PASSWORD (systemctl edit ${SERVICE_NAME}) — the server REFUSES a public"
echo "    bind without auth. Then open the operator IP range to TCP ${PORT} in the host SG."
echo "  * Service starts read-only (NODEOPS_READONLY=1); flip it once validated."
echo "  * Check status: systemctl status ${SERVICE_NAME}"
