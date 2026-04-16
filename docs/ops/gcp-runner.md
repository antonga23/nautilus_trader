# GCP CI Runner Runbook

## Role and labels

- Role: primary self-hosted CI runner for this repository
- Expected labels: `self-hosted`, `Linux`, `X64`, `cloudbet-market-maker`, `ci`, `gcp`, `ubuntu24`
- Expected OS: Ubuntu 24.04

## Key services

- GitHub Actions runner service: `actions.runner.antonga23-cloudbet-market-maker.<runner-name>.service`
- Docker service: `docker.service`
- Optional hygiene timer if installed: `actions-runner-hygiene.timer`

Discover the exact runner service name with:

```bash
systemctl list-units 'actions.runner.antonga23-cloudbet-market-maker*.service'
```

## Health checks

- `df -hP /`
- `docker ps -a`
- `systemctl status "actions.runner.antonga23-cloudbet-market-maker.<runner-name>.service"`
- from a checkout of this repository on the host:
  `RUNNER_ROLE=ci ACTIONS_RUNNER_ROOT=/opt/actions-runner GITHUB_RUNNER_SERVICE_NAME="actions.runner.antonga23-cloudbet-market-maker.<runner-name>.service" bash scripts/ci/runner_health_check.sh`

The health script reports:

- root disk headroom
- runner root, `_diag`, and local cache usage
- Docker availability
- `bash`, `python3`, `git`, `uv`, `gcc`, and `clang`
- runner service state

## Drain the runner

1. Let the active CI job finish or cancel it from GitHub Actions.
2. Stop the runner service: `sudo systemctl stop "actions.runner.antonga23-cloudbet-market-maker.<runner-name>.service"`
3. Confirm no CI containers are still active: `docker ps -a`
4. Perform cleanup if `_diag`, `_work`, or `.ci-cache` growth is abnormal.

## Restart the runner

1. Start the runner service: `sudo systemctl start "actions.runner.antonga23-cloudbet-market-maker.<runner-name>.service"`
2. Confirm the runner is back online in the repository runner settings.
3. Re-run the health script from a local checkout with the service name exported.

## Cleanup

- inspect runner usage from a checkout of this repository:
  `RUNNER_ROLE=ci ACTIONS_RUNNER_ROOT=/opt/actions-runner GITHUB_RUNNER_SERVICE_NAME="actions.runner.antonga23-cloudbet-market-maker.<runner-name>.service" bash scripts/ci/runner_health_check.sh`
- remove stale runner diagnostics manually if `_diag` grows unexpectedly
- remove stale runner `_work/_temp` content only while the runner service is stopped
- prune stopped containers and unused images if disk pressure rises

## Bootstrap requirements

Fresh CI runners must be provisioned with the package manifest from
[`self-hosted-runner-packages.txt`](../../.docker/self-hosted-runner-packages.txt)
through
[`bootstrap-self-hosted-runner-host.sh`](../../.docker/bootstrap-self-hosted-runner-host.sh).
That manifest now includes the native compiler toolchain and headers required
for:

- `cargo install cargo-vet`
- `cargo install cargo-deny`
- Cap'n Proto source builds during `common-setup`

At minimum, a healthy GCP CI runner should have `gcc`, `clang`,
`build-essential`, `cmake`, `libssl-dev`, and `zlib1g-dev` available before it
starts taking jobs.

## Cache behavior

- GitHub Actions caches created with `actions/cache` are stored in GitHub's cache backend and can be restored on either GCP or EC2 when the cache key matches.
- Runner-local wheel cache under `.ci-cache/wheels` is host-local. Wheels built on GCP are not available on EC2 unless the job also hits a GitHub-managed cache.
- Workflow artifacts are per-run only. They can move between jobs in the same workflow run, but they are not a future-run cache.

## Caveats

- General CI should stay pinned to the GCP label set. If EC2 still carries the CI labels, jobs can land on the wrong host.
- Cross-runner cache sharing is partial by design. Do not assume EC2 can reuse GCP's local `.ci-cache/wheels`.
