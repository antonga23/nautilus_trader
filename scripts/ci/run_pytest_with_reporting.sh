#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <job-name> <junit-xml> <log-path> [pytest args...]" >&2
  exit 64
fi

job_name="$1"
junit_xml="$2"
log_path="$3"
shift 3
summary_path="${GITHUB_STEP_SUMMARY:-}"
mkdir -p "$(dirname "$junit_xml")" "$(dirname "$log_path")"

set +e
uv run --no-sync pytest --junitxml="$junit_xml" "$@" 2>&1 | tee "$log_path"
status=${PIPESTATUS[0]}
set -e

xdist_crash=false
if grep -Eq 'Fatal Python error:|node down: Not properly terminated|worker .*crashed|replacing crashed worker' "$log_path"; then
  xdist_crash=true
  echo "::error title=${job_name} xdist worker crash::Detected an abnormal xdist worker termination; inspect ${log_path}"
fi

failure_classification="success"
if [[ "$status" -ne 0 ]]; then
  if [[ "$xdist_crash" == true ]]; then
    failure_classification="worker-crash"
  elif [[ -f "$junit_xml" ]]; then
    failure_classification="test-failure"
  else
    failure_classification="infra-failure"
  fi
fi

junit_summary=$(
  python3 - "$junit_xml" << 'PY'
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("tests=0 errors=0 failures=0 skipped=0 time=0")
    raise SystemExit(0)

root = ET.parse(path).getroot()
if root.tag == "testsuites":
    suites = [root]
else:
    suites = [root]

def total(attr: str) -> str:
    value = 0.0
    for suite in suites:
        raw = suite.attrib.get(attr, "0")
        try:
            value += float(raw)
        except ValueError:
            pass
    if attr == "time":
        return f"{value:.3f}"
    return str(int(value))

print(
    f"tests={total('tests')} errors={total('errors')} failures={total('failures')} "
    f"skipped={total('skipped')} time={total('time')}"
)
PY
)

if [[ -n "$summary_path" ]]; then
  {
    echo "## ${job_name}"
    echo
    echo "- pytest exit code: ${status}"
    echo "- classification: ${failure_classification}"
    echo "- junit: ${junit_xml}"
    echo "- log: ${log_path}"
    echo "- junit summary: ${junit_summary}"
    echo "- xdist worker crash detected: ${xdist_crash}"
    if [[ "$xdist_crash" == true ]]; then
      echo
      echo "> Detected an abnormal xdist worker termination. Inspect the crash excerpt below and the raw log artifact."
      echo
      echo '```text'
      grep -E -C 3 'Fatal Python error:|node down: Not properly terminated|worker .*crashed|replacing crashed worker' "$log_path" | tail -n 40 || true
      echo '```'
    fi
  } >> "$summary_path"
fi

exit "$status"
