# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Secret-manager helpers for semantic mining operator workflows.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess


class SecretManagerError(RuntimeError):
    pass


def load_aws_secret_payload(
    *,
    secret_id: str,
    region: str | None = None,
    runner=subprocess.run,
) -> dict[str, object]:
    cmd = [
        "aws",
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        secret_id,
        "--query",
        "SecretString",
        "--output",
        "text",
    ]
    if region:
        cmd.extend(["--region", region])
    completed = runner(cmd, check=True, capture_output=True, text=True)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SecretManagerError(f"Invalid secret payload for {secret_id}") from exc


def restore_gcp_service_account(
    *,
    payload: dict[str, object],
    output_path: str | Path,
    secret_key: str = "GCP_SERVICE_ACCOUNT_JSON_B64",  # noqa: S107
) -> Path:
    encoded = payload.get(secret_key)
    if not isinstance(encoded, str) or not encoded:
        raise SecretManagerError(f"Missing {secret_key} in secret payload")

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(encoded))
    return target
