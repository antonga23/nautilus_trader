#!/usr/bin/env bash
set -euo pipefail

workspace_root="${GITHUB_WORKSPACE:-$PWD}"
python_bin="${workspace_root}/.venv/bin/python"

if [[ ! -x "$python_bin" ]]; then
  echo "Expected virtualenv python at ${python_bin}" >&2
  exit 1
fi

smoke_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$smoke_dir"
}
trap cleanup EXIT

(
  cd "$smoke_dir"
  GITHUB_WORKSPACE="$workspace_root" "$python_bin" - <<'PY'
import os
from pathlib import Path

import nautilus_trader
from nautilus_trader.core import nautilus_pyo3

workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
package_path = Path(nautilus_trader.__file__).resolve()
extension_path = Path(nautilus_pyo3.__file__).resolve()

print(package_path)
print(extension_path)

def ensure_installed(path: Path) -> None:
    try:
        relative = path.relative_to(workspace)
    except ValueError as exc:
        raise SystemExit(f"Imported path is outside the workspace: {path}") from exc

    if not relative.parts or relative.parts[0] != ".venv":
        raise SystemExit(
            "Smoke test imported from the checkout instead of the installed wheel: "
            f"{path}"
        )

ensure_installed(package_path)
ensure_installed(extension_path)
PY
)
