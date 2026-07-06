# Global Agent Rules

- GitHub identity for this repo is `antonga23` (personal, non-Pencil). Multiple agents
  share the global gh/git state, so **do not** use `gh auth switch` (it races other
  sessions). Select the account per command:
  `GH_TOKEN="$(gh auth token --user antonga23)" gh …` — this also works for git over
  HTTPS (origin uses the `gh auth git-credential` helper, which honours `GH_TOKEN`).
  For pure git, the `github-personal` SSH alias (`~/.ssh/github_personal`) →
  `git@github-personal:antonga23/…` is equivalent. AWS: always pass
  `--profile betting-project`. All pushes/PRs target `antonga23/cloudbet-market-maker`;
  never push to upstream Nautilus (`nautechsystems/nautilus_trader`) or any Pencil account.
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
  - Dedicated remote code-dev VMs are the only acceptable surface for manual
    build/test/pre-commit/semantic-completion work when a runner workflow is not
    appropriate.
  - The local Mac is edit/light-inspection only. Do not run resource-heavy
    build, pre-commit, pytest, ruff, Rust, wheel, Docker, semantic completion,
    or strategy-node image workloads locally.
  - Substantial Codex worktrees should be created on the remote code-dev VM
    rather than under the local Codex worktree tree.
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
