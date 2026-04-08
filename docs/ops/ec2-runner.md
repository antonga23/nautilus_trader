# EC2 Runner Runbook

## Services

- GitHub Actions runner: `actions.runner.antonga23-cloudbet-market-maker.EC2-Runner.service`
- Symphony control plane: `control-plane.service`
- Symphony detached watchdog: user cron invoking `/srv/symphony/control-repo/scripts/symphony/ensure_running.sh`
- Runner hygiene timer: `actions-runner-hygiene.timer`

## Health checks

- `df -hT /`
- `docker ps -a`
- `du -sh /home/ubuntu/actions-runner/_diag`
- `du -sh /home/ubuntu/actions-runner/_work/.ci-cache`
- `du -xhd1 /home/ubuntu/actions-runner/_work | sort -h`
- `du -xhd1 /srv/symphony/workspaces | sort -h`
- `systemctl status actions.runner.antonga23-cloudbet-market-maker.EC2-Runner.service`
- `systemctl status control-plane.service`
- `systemctl status actions-runner-hygiene.timer`
- `bash /srv/symphony/control-repo/scripts/ci/runner_health_check.sh`

## Drain the runner

1. Cancel or let the active GitHub Actions job finish.
2. Stop the runner service: `sudo systemctl stop actions.runner.antonga23-cloudbet-market-maker.EC2-Runner.service`
3. Confirm `docker ps -a` has no active CI containers.
4. Run hygiene cleanup if disk pressure or stale logs are present.

## Restart the runner

1. Start the runner service: `sudo systemctl start actions.runner.antonga23-cloudbet-market-maker.EC2-Runner.service`
2. Confirm it is online in GitHub repository runner settings.
3. Confirm the hygiene timer is still enabled.

## Cleanup

- one-shot cleanup: `sudo bash /srv/symphony/control-repo/scripts/ci/self_hosted_runner_cleanup.sh`
- install hourly cleanup timer: `sudo bash /srv/symphony/control-repo/scripts/ci/install_runner_hygiene.sh`
- print a one-shot health summary: `bash /srv/symphony/control-repo/scripts/ci/runner_health_check.sh`

## What the hygiene timer does

- prunes exited Docker containers
- prunes unused Docker images and volumes when root disk usage is above the configured threshold
- deletes runner `_diag` files older than the configured retention window
- trims `_diag` further if it grows beyond the configured size cap
- deletes stale Actions runner `_work/_temp` scratch data
- removes stale `_github_home`, `_github_workflow`, and `_runner_file_commands` when the runner is idle so root-owned container temp cannot bleed into later jobs
- deletes stale Actions runner work directories older than the configured retention window
- deletes stale runner-local CI caches under `/home/ubuntu/actions-runner/_work/.ci-cache`
- deletes stale `.tmp-precommit` scratch data inside Symphony workspaces
- deletes stale Symphony workspaces older than the configured retention window
- deletes stale derived artifacts in `/srv/symphony/control-repo` (for example `target`, `dist`, `.venv`, and caches)

## Symptoms to watch

- large growth in `/home/ubuntu/actions-runner/_diag`
- exited Docker job containers accumulating
- `/home/ubuntu/actions-runner/_work/_temp` growing rapidly
- stale runner work directories under `/home/ubuntu/actions-runner/_work`
- runaway growth under `/home/ubuntu/actions-runner/_work/.ci-cache`
- repeated wheel-build or Cargo registry failures referencing `/github/home/.cargo/...`
- stale `.tmp-precommit` directories in `/srv/symphony/workspaces`
- stale Symphony issue workspaces older than a week
- runner disk usage above 85%
- repeated cancellation delays on superseded GitHub Actions runs
