#!/usr/bin/env bash
set -euo pipefail

cd /srv/symphony/control-repo
./scripts/symphony/render_env_from_secret.sh
exec ./scripts/symphony/run_service.sh
