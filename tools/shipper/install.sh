#!/usr/bin/env bash
# Idempotent installer for the nodeops data shipper on the strategy-node deploy host.
#
# Creates /opt/cloudbet/shipper/venv, installs psycopg[binary], copies shipper.py +
# schema.sql, installs the systemd unit (only if absent — existing unit kept so local
# edits survive a re-run), then enables and starts the service. Purely additive: it
# never touches the nodeops service or the trading nodes.
#
# DB credentials are read at runtime from /opt/cloudbet/shipper/shipper.env, which you
# must create BEFORE first start (chmod 600, root-owned). It is NOT committed. Example:
#
#   SHIPPER_PG_HOST=my-instance.abc123.eu-west-1.rds.amazonaws.com
#   SHIPPER_PG_PORT=5432
#   SHIPPER_PG_DATABASE=nodeops
#   SHIPPER_PG_USER=nodeops_shipper
#   SHIPPER_PG_PASSWORD=...
#   SHIPPER_PG_SSLMODE=require
#   NODEOPS_DB=/opt/cloudbet/nodeops/nodeops.db
#   NODES_ROOT=/opt/cloudbet/strategy-nodes
#   SHIPPER_INTERVAL_SECS=30
set -euo pipefail

INSTALL_DIR="/opt/cloudbet/shipper"
VENV_DIR="${INSTALL_DIR}/venv"
ENV_FILE="${INSTALL_DIR}/shipper.env"
UNIT_SRC_NAME="shipper.service"
UNIT_DEST="/etc/systemd/system/shipper.service"
SERVICE_NAME="shipper"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install.sh must run as root (sudo)." >&2
  exit 1
fi

echo "Installing shipper into ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating venv ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip
"${VENV_DIR}/bin/python" -m pip install --quiet "psycopg[binary]"

install -m 0644 "${SCRIPT_DIR}/shipper.py" "${INSTALL_DIR}/shipper.py"
install -m 0644 "${SCRIPT_DIR}/schema.sql" "${INSTALL_DIR}/schema.sql"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "WARNING: ${ENV_FILE} does not exist. Create it (chmod 600, root) with the RDS"
  echo "         credentials before the service can connect. See the header of this script."
  install -m 0600 /dev/null "${ENV_FILE}"
  echo "# Fill in SHIPPER_PG_* / NODEOPS_DB / NODES_ROOT — see install.sh header." > "${ENV_FILE}"
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
echo "shipper installed. It reads ${ENV_FILE} for RDS creds."
echo "  * Check status:  systemctl status ${SERVICE_NAME}"
echo "  * Follow logs:   journalctl -u ${SERVICE_NAME} -f"
echo "  * The shipper is additive: nodeops + trading nodes are untouched and not restarted."
