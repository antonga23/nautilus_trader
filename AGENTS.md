# Global Agent Rules

- For any monitoring or supervision task expected to run longer than 60 seconds,
  use the `background-monitor` skill instead of model-loop polling.
- Do not use repeated `sleep`, `gh`, `tail`, or SSH polling commands from the
  conversation to watch CI, logs, remote builds, or deploys.
- Prefer one watcher process that exits only on the desired condition:
  first failure, terminal success/failure, matching error log line, or remote
  command completion.
- Persist watcher output to a durable log path whenever the command may outlive
  the current interaction.
- Runner ownership is strict:
  - GCP self-hosted runners are the primary CI surface for pre-commit, Python
    tests, Rust policy checks, wheel builds, and strategy-node image builds.
  - EC2 is the deploy/trading host only. Use it for strategy-node release,
    runtime, lifecycle, health, and log inspection work.
- Do not move pre-commit, build, test, or image-build work to EC2 as a fallback
  when GCP auth or runner access fails. Re-authenticate GCP or use the intended
  GitHub Actions workflow instead.
- Before spending a GitHub Actions run for Python validation, prefer the
  `ci-preflight` skill and run the same validation slice on the GCP CI runner
  when that runner is reachable.
- For multi-part plans, release/runtime recovery, CI migrations, or any request
  to drive work to completion without stopping, use the `end-to-end-completion`
  skill. Maintain a requirements ledger, let newer requirements supersede older
  conflicting ones, exhaust safe workarounds before escalating, and verify every
  requested outcome before handing work back.
- Do not persist plaintext cloud passwords in repo files, skills, scripts, logs,
  or documentation. Use interactive auth, short-lived credentials, or an
  approved secret manager.
- Before claiming semantic betting rule mining or market-semantics work is
  complete, use the `semantic-rule-mining-completion` skill and include the
  `verify-completion` result. Candidate counts, template counts, promotions,
  and execution-safe templates must be reported separately.
