#!/usr/bin/env python3
"""
Dependency-free reader for the trading-host registry (deploy/hosts.*).

Used by scripts/host/hostctl.sh so bash never parses YAML. Accepts BOTH:

- JSON (`deploy/hosts.json`) — parsed with the stdlib, any valid JSON works.
- YAML (`deploy/hosts.yaml`) — parsed with a small constrained parser (PyYAML may
  be absent on operator machines). The file must keep the exact shape of
  `deploy/hosts.example.yaml`: 2-space indentation, a top-level `version` scalar
  and `hosts` list of maps, one nested map per host (`ssh`), one nested scalar
  list (`labels`), `#` comments, and no multi-line/flow/anchor syntax. Anything
  fancier should be committed as JSON instead.

Subcommands:
  list <file>              one aligned row per host (name, ssh, provider, region, labels)
  get <file> <name>        `key=value` lines for one host (consumed by hostctl.sh)
  self-test <example-file> parse assertions against the committed example; exit 0/1

"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NoReturn

DEFAULT_NODES_ROOT = "/opt/cloudbet/strategy-nodes"


def _strip_comment(line: str) -> str:
    # A '#' starts a comment at line start or after whitespace. Values containing
    # '#' are not supported by this constrained parser (use JSON for those).
    for i, ch in enumerate(line):
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value in ("true", "false"):
        return value == "true"
    return value


def _fail(lineno: int, raw: str, reason: str) -> NoReturn:
    raise ValueError(
        f"line {lineno}: {reason} (constrained schema — see deploy/README.md): {raw!r}",
    )


class _HostsYamlParser:
    """
    Line-driven parser for the constrained hosts.yaml shape (see module docstring).
    """

    def __init__(self) -> None:
        self.version: object = None
        self.hosts: list[dict] = []
        self.current: dict | None = None
        self.sub_key: str | None = None

    def feed(self, lineno: int, raw: str) -> None:
        line = _strip_comment(raw)
        if not line.strip():
            return
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if indent == 0:
            self._top_level(lineno, raw, content)
        elif indent == 2 and content.startswith("- "):
            self._host_item(lineno, raw, content)
        elif indent == 4:
            self._host_field(lineno, raw, content)
        elif indent >= 6 and self.sub_key is not None:
            self._sub_field(lineno, raw, content)
        else:
            _fail(lineno, raw, "unsupported indentation or nesting")

    def _top_level(self, lineno: int, raw: str, content: str) -> None:
        self.sub_key = None
        key, sep, value = content.partition(":")
        if not sep:
            _fail(lineno, raw, "expected 'key: value'")
        if key == "version":
            self.version = _scalar(value)
        elif key == "hosts" and not value.strip():
            pass
        else:
            _fail(lineno, raw, "only 'version' and 'hosts:' are allowed at top level")

    def _host_item(self, lineno: int, raw: str, content: str) -> None:
        self.current = {}
        self.hosts.append(self.current)
        self.sub_key = None
        key, sep, value = content[2:].partition(":")
        if not sep or not value.strip():
            _fail(lineno, raw, "a host item must start with '- key: value'")
        self.current[key.strip()] = _scalar(value)

    def _host_field(self, lineno: int, raw: str, content: str) -> None:
        if self.current is None:
            _fail(lineno, raw, "host fields before any '- name:' item")
        key, sep, value = content.partition(":")
        if not sep:
            _fail(lineno, raw, "expected 'key: value'")
        key = key.strip()
        if value.strip():
            self.current[key] = _scalar(value)
            self.sub_key = None
        else:
            self.current[key] = None  # container type decided by the first child
            self.sub_key = key

    def _sub_field(self, lineno: int, raw: str, content: str) -> None:
        if self.current is None or self.sub_key is None:
            _fail(lineno, raw, "nested fields outside a host item")
        container = self.current[self.sub_key]
        if content.startswith("- "):
            if container is None:
                container = self.current[self.sub_key] = []
            if not isinstance(container, list):
                _fail(lineno, raw, f"'{self.sub_key}' mixes list and map children")
            container.append(_scalar(content[2:]))
        else:
            if container is None:
                container = self.current[self.sub_key] = {}
            if not isinstance(container, dict):
                _fail(lineno, raw, f"'{self.sub_key}' mixes list and map children")
            key, sep, value = content.partition(":")
            if not sep:
                _fail(lineno, raw, "expected 'key: value'")
            container[key.strip()] = _scalar(value)


def parse_hosts_yaml(text: str) -> dict:
    parser = _HostsYamlParser()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        parser.feed(lineno, raw)
    return {"version": parser.version, "hosts": parser.hosts}


def load_registry(path: str) -> dict:
    text = Path(path).read_text(encoding="utf8")
    if path.endswith(".json"):
        data = json.loads(text)
    else:
        data = parse_hosts_yaml(text)
    if not isinstance(data.get("hosts"), list):
        raise ValueError(f"{path}: missing 'hosts' list")
    for host in data["hosts"]:
        for field in ("name", "kind"):
            if not host.get(field):
                raise ValueError(f"{path}: host record missing '{field}': {host}")
        if host["kind"] != "ssh":
            raise ValueError(f"{path}: host {host['name']!r}: only kind 'ssh' is supported")
        ssh = host.get("ssh") or {}
        if not ssh.get("host") or not ssh.get("user"):
            raise ValueError(f"{path}: host {host['name']!r}: ssh.host and ssh.user are required")
    return data


def find_host(data: dict, name: str) -> dict:
    for host in data["hosts"]:
        if host["name"] == name:
            return host
    known = ", ".join(h["name"] for h in data["hosts"]) or "<none>"
    raise KeyError(f"host {name!r} not in registry (known: {known})")


def cmd_list(path: str) -> int:
    data = load_registry(path)
    rows = [("NAME", "SSH", "PROVIDER", "REGION", "LABELS")]
    for host in data["hosts"]:
        ssh = host["ssh"]
        rows.append(
            (
                host["name"],
                f"{ssh['user']}@{ssh['host']}",
                str(host.get("provider", "-")),
                str(host.get("region", "-")),
                ",".join(host.get("labels") or []) or "-",
            ),
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return 0


def cmd_get(path: str, name: str) -> int:
    host = find_host(load_registry(path), name)
    ssh = host["ssh"]
    fields = {
        "name": host["name"],
        "ssh_host": ssh["host"],
        "ssh_user": ssh["user"],
        "identity_file_hint": ssh.get("identity_file_hint", ""),
        "provider": host.get("provider", ""),
        "region": host.get("region", ""),
        "nodes_root": host.get("nodes_root") or DEFAULT_NODES_ROOT,
        "nodeops_url": host.get("nodeops_url", ""),
        "labels": ",".join(host.get("labels") or []),
    }
    for key, value in fields.items():
        if "\n" in str(value):
            raise ValueError(f"{path}: host {name!r}: field {key!r} contains a newline")
        print(f"{key}={value}")
    return 0


def cmd_self_test(example_path: str) -> int:
    failures = 0

    def check(label: str, expected, actual):
        nonlocal failures
        if expected == actual:
            print(f"ok   registry/{label}")
        else:
            print(f"FAIL registry/{label} (expected={expected!r} actual={actual!r})")
            failures += 1

    data = load_registry(example_path)
    check("version", 1, data["version"])
    check("host_count", 2, len(data["hosts"]))

    ec2 = find_host(data, "betting-dev-ec2")
    check("ec2/kind", "ssh", ec2["kind"])
    check("ec2/ssh_host", "203.0.113.10", ec2["ssh"]["host"])
    check("ec2/ssh_user", "ubuntu", ec2["ssh"]["user"])
    check("ec2/identity_hint", "~/.ssh/betting-dev-ec2.pem", ec2["ssh"]["identity_file_hint"])
    check("ec2/provider", "aws", ec2["provider"])
    check("ec2/nodes_root", DEFAULT_NODES_ROOT, ec2["nodes_root"])
    check("ec2/labels", ["dev", "primary"], ec2["labels"])

    gcp = find_host(data, "betting-dev-gcp")
    check("gcp/ssh_user", "cloudbet", gcp["ssh"]["user"])
    check("gcp/provider", "gcp", gcp["provider"])
    check("gcp/labels", ["dev", "spare"], gcp["labels"])

    # The constrained YAML parse and a JSON round-trip must agree exactly, proving
    # hosts.json is a drop-in replacement for hosts.yaml.
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(data, handle)
        json_path = handle.name
    try:
        check("json_roundtrip", data, load_registry(json_path))
    finally:
        Path(json_path).unlink()

    try:
        find_host(data, "no-such-host")
        check("missing_host_raises", True, False)
    except KeyError:
        check("missing_host_raises", True, True)

    print(f"registry self-test: {failures} failure(s)")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "list":
        return cmd_list(argv[2])
    if len(argv) >= 4 and argv[1] == "get":
        return cmd_get(argv[2], argv[3])
    if len(argv) >= 3 and argv[1] == "self-test":
        return cmd_self_test(argv[2])
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (OSError, ValueError, KeyError) as exc:
        print(f"registry.py: {exc}", file=sys.stderr)
        sys.exit(1)
