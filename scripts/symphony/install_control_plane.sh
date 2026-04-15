#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
control_plane_dir="$repo_root/scripts/symphony/control_plane"
node_version="${CONTROL_PLANE_NODE_VERSION:-20.19.0}"
control_plane_domain="${CONTROL_PLANE_DOMAIN:-controlplane.cheapestgames.online}"
certbot_email="${CONTROL_PLANE_CERTBOT_EMAIL:-${LETSENCRYPT_EMAIL:-}}"

install_node_if_needed() {
  local current_major=""
  if command -v node >/dev/null 2>&1; then
    current_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || true)"
  fi
  if [ -n "$current_major" ] && [ "$current_major" -ge 20 ]; then
    return 0
  fi

  local arch platform tarball url tmpdir install_dir
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) platform="linux-x64" ;;
    aarch64|arm64) platform="linux-arm64" ;;
    *) echo "Unsupported Node architecture: $arch" >&2; exit 1 ;;
  esac
  tarball="node-v${node_version}-${platform}.tar.xz"
  url="https://nodejs.org/dist/v${node_version}/${tarball}"
  tmpdir="$(mktemp -d)"
  install_dir="/opt/node-v${node_version}-${platform}"

  echo "Installing Node.js ${node_version} for ${platform}" >&2
  curl -fsSL "$url" -o "$tmpdir/$tarball"
  sudo rm -rf "$install_dir"
  sudo tar -xJf "$tmpdir/$tarball" -C /opt
  sudo ln -sfn "$install_dir/bin/node" /usr/local/bin/node
  sudo ln -sfn "$install_dir/bin/npm" /usr/local/bin/npm
  sudo ln -sfn "$install_dir/bin/npx" /usr/local/bin/npx
  rm -rf "$tmpdir"
}

public_ip() {
  curl -fsS --max-time 3 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true
}

resolved_ip() {
  python3 - "$control_plane_domain" <<'PY' 2>/dev/null || true
import socket, sys
try:
    print(socket.gethostbyname(sys.argv[1]))
except Exception:
    pass
PY
}

run_certbot_if_ready() {
  if [ -z "$control_plane_domain" ] || [[ "$control_plane_domain" =~ ^[0-9.]+$ ]]; then
    echo "Skipping certbot: CONTROL_PLANE_DOMAIN must be a DNS hostname, not an IP." >&2
    return 0
  fi

  local resolved current_public
  resolved="$(resolved_ip)"
  current_public="$(public_ip)"
  if [ -z "$resolved" ] || [ -z "$current_public" ] || [ "$resolved" != "$current_public" ]; then
    echo "Skipping certbot: ${control_plane_domain} resolves to '${resolved:-unresolved}', this host public IP is '${current_public:-unknown}'." >&2
    echo "Point ${control_plane_domain} at this EC2 public IP, then rerun this installer." >&2
    return 0
  fi

  if ! command -v certbot >/dev/null 2>&1; then
    sudo apt-get update -o Acquire::Retries=5
    sudo apt-get install -y certbot python3-certbot-nginx -o Acquire::Retries=5
  fi

  local email_args=(--register-unsafely-without-email)
  if [ -n "$certbot_email" ]; then
    email_args=(-m "$certbot_email")
  fi

  sudo certbot --nginx --non-interactive --agree-tos --redirect "${email_args[@]}" -d "$control_plane_domain"
}

cd "$repo_root"
./scripts/symphony/render_env_from_secret.sh
set -a
# shellcheck disable=SC1091
source /srv/symphony/symphony.env
set +a

if [ -z "${SYMPHONY_DASHBOARD_USER:-}" ] || [ -z "${SYMPHONY_DASHBOARD_PASSWORD:-}" ]; then
  echo "Missing SYMPHONY_DASHBOARD_USER or SYMPHONY_DASHBOARD_PASSWORD in rendered env" >&2
  exit 1
fi

install_node_if_needed

(
  cd "$control_plane_dir"
  npm ci
  npm run build
)

hash_value="$(openssl passwd -apr1 "$SYMPHONY_DASHBOARD_PASSWORD")"
printf '%s:%s\n' "$SYMPHONY_DASHBOARD_USER" "$hash_value" | sudo tee /etc/nginx/.htpasswd-symphony >/dev/null
sudo chown root:www-data /etc/nginx/.htpasswd-symphony
sudo chmod 640 /etc/nginx/.htpasswd-symphony

sudo install -d -m 755 /var/www/letsencrypt
sudo install -m 644 "$repo_root/scripts/symphony/control-plane.service" /etc/systemd/system/control-plane.service
sed "s/__CONTROL_PLANE_SERVER_NAME__/${control_plane_domain}/g" "$repo_root/scripts/symphony/control-plane.nginx.conf" | sudo tee /etc/nginx/sites-available/symphony-control-plane >/dev/null
sudo rm -f /etc/nginx/sites-enabled/symphony-dashboard /etc/nginx/sites-enabled/symphony-control-plane
sudo rm -f /etc/nginx/sites-available/symphony-dashboard
sudo ln -sfn /etc/nginx/sites-available/symphony-control-plane /etc/nginx/sites-enabled/symphony-control-plane

sudo systemctl daemon-reload
sudo systemctl enable --now control-plane.service
sudo nginx -t
sudo systemctl reload nginx
run_certbot_if_ready
sudo nginx -t
sudo systemctl reload nginx
