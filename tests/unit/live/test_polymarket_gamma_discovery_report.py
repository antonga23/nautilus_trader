# skipcq: PYL-C0114, PYL-C0115, PYL-C0116

import json

from scripts.betting import polymarket_gamma_discovery_report as report


def test_summarize_sports_metadata_groups_alias_tags():
    payload = [
        {"sport": "atp", "tags": "1,864,100639,101232"},
        {"sport": "wta", "tags": "1,864,100639,102123"},
        {"sport": "nba", "tags": "1,745,100639"},
        "ignored",
    ]

    summary = report.summarize_sports_metadata(
        payload,
        requested_sports={"tennis", "basketball", "soccer"},
    )

    assert summary["skippedRows"] == 1
    assert summary["unresolvedRequestedSports"] == ["soccer"]
    assert summary["sports"]["tennis"] == {
        "sportCodes": ["atp", "wta"],
        "tagIds": ["864", "101232", "102123"],
        "tagCount": 3,
    }
    assert summary["sports"]["basketball"] == {
        "sportCodes": ["nba"],
        "tagIds": ["745"],
        "tagCount": 1,
    }


def test_cli_reads_saved_payload(tmp_path, capsys):
    payload_path = tmp_path / "sports.json"
    payload_path.write_text(json.dumps([{"sport": "atp", "tags": "864"}]))

    rc = report.main(["--sports-json", str(payload_path), "--sport", "tennis"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "polymarket gamma discovery" in output
    assert "tennis: codes=['atp'] tags=['864']" in output
