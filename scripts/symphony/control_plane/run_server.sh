#!/usr/bin/env bash
set -euo pipefail

cd /srv/symphony/control-repo
./scripts/symphony/render_env_from_secret.sh
set -a
# shellcheck source=/dev/null
source /srv/symphony/symphony.env
set +a

if [ -f /srv/symphony/control-repo/scripts/symphony/control_plane/package.json ] &&
  [ ! -f /srv/symphony/control-repo/scripts/symphony/control_plane/dist/index.html ]; then
  (
    cd /srv/symphony/control-repo/scripts/symphony/control_plane
    npm ci
    npm run build
  )
fi

exec /usr/bin/env node /srv/symphony/control-repo/scripts/symphony/control_plane/server.mjs
