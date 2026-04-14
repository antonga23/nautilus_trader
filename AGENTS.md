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
- Before spending a GitHub Actions run for Python validation, prefer the
  `ci-preflight` skill and run the same validation slice on the EC2 runner when
  the runner is reachable.
