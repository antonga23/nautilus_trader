# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import base64
from pathlib import Path

from nautilus_trader.adapters.betting.semantics.secrets import load_aws_secret_payload
from nautilus_trader.adapters.betting.semantics.secrets import restore_gcp_service_account


class _Completed:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_load_aws_secret_payload_uses_cli_runner():
    observed = {}

    def fake_runner(cmd, check, capture_output, text):
        observed["cmd"] = cmd
        assert check is True
        assert capture_output is True
        assert text is True
        return _Completed('{"GCP_SERVICE_ACCOUNT_JSON_B64":"abc"}')

    payload = load_aws_secret_payload(
        secret_id="cloudbet-market-maker/credentials",
        region="us-east-1",
        runner=fake_runner,
    )

    assert payload["GCP_SERVICE_ACCOUNT_JSON_B64"] == "abc"
    assert "--region" in observed["cmd"]


def test_restore_gcp_service_account_decodes_base64(tmp_path: Path):
    payload = {
        "GCP_SERVICE_ACCOUNT_JSON_B64": base64.b64encode(
            b'{"type":"service_account","project_id":"demo"}',
        ).decode("ascii"),
    }

    restored = restore_gcp_service_account(
        payload=payload,
        output_path=tmp_path / "gcp.json",
    )

    assert restored.read_text(encoding="utf-8") == '{"type":"service_account","project_id":"demo"}'
