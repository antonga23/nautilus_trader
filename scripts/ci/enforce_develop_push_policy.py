#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import urllib.parse
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NULL_SHA = "0" * 40
GUARD_COMMIT_PREFIX = "[develop-guard]"
GUARD_BOT_LOGIN = "github-actions[bot]"
DEFAULT_ACCEPT = "application/vnd.github+json"


@dataclass(frozen=True)
class CommitValidation:
    sha: str
    message: str
    valid: bool
    reason: str
    pr_number: int | None = None
    pr_head_sha: str | None = None
    pr_validation_run_id: int | None = None


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str
    before: str
    after: str
    forced: bool
    pr_number: int | None
    invalid_commits: list[str]
    validations: list[CommitValidation]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["validations"] = [asdict(item) for item in self.validations]
        return payload


class GitHubApiClient:
    def __init__(self, token: str, *, api_url: str = "https://api.github.com") -> None:
        parsed = urllib.parse.urlparse(api_url)
        if parsed.scheme != "https":
            raise ValueError(f"GitHub API URL must use https, got {api_url!r}")
        if not parsed.netloc:
            raise ValueError(f"GitHub API URL must include a host, got {api_url!r}")
        self._token = token
        self._api_host = parsed.netloc
        self._api_prefix = parsed.path.rstrip("/")

    def _request_json(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        accept: str = DEFAULT_ACCEPT,
    ) -> Any:
        request_path = f"{self._api_prefix}{path}"
        if query:
            request_path = f"{request_path}?{urllib.parse.urlencode(query, doseq=True)}"

        # skipcq: BAN-B309
        connection = http.client.HTTPSConnection(
            self._api_host,
            timeout=60,
            context=ssl.create_default_context(),
        )
        response: http.client.HTTPResponse | None = None
        try:
            connection.request(
                "GET",
                request_path,
                headers={
                    "Accept": accept,
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": "cloudbet-market-maker-develop-guard",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response = connection.getresponse()
            response_status = response.status
            payload = response.read().decode("utf-8", errors="replace")
        except OSError as e:  # pragma: no cover - surfaced via CLI exit
            raise RuntimeError(f"GitHub API request failed for {path}: {e}") from e
        finally:
            if response is not None:
                response.close()
            connection.close()

        if response_status >= 400:
            raise RuntimeError(f"GitHub API {response_status} for {path}: {payload}")
        if not payload:
            return {}
        return json.loads(payload)

    def compare_commits(self, repo: str, before: str, after: str) -> dict[str, Any]:
        return self._request_json(f"/repos/{repo}/compare/{before}...{after}")

    def associated_pull_requests(self, repo: str, commit_sha: str) -> list[dict[str, Any]]:
        return self._request_json(f"/repos/{repo}/commits/{commit_sha}/pulls")

    def workflow_runs_for_head_sha(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"/repos/{repo}/actions/runs",
            query={"head_sha": head_sha, "per_page": 100},
        )
        return payload.get("workflow_runs", [])


def _normalize_commit(commit: dict[str, Any]) -> dict[str, str]:
    sha = str(commit.get("sha") or commit.get("id") or "")
    message = str(commit.get("message") or commit.get("commit", {}).get("message") or "")
    return {"sha": sha, "message": message}


def _is_guard_revert_push(event: dict[str, Any]) -> bool:
    sender = str(event.get("sender", {}).get("login") or "")
    head_message = str(event.get("head_commit", {}).get("message") or "")
    return sender == GUARD_BOT_LOGIN and head_message.startswith(GUARD_COMMIT_PREFIX)


def _list_push_commits(
    client: GitHubApiClient,
    repo: str,
    event: dict[str, Any],
) -> list[dict[str, str]]:
    before = str(event.get("before") or "")
    after = str(event.get("after") or "")
    if before and before != NULL_SHA and after and after != NULL_SHA:
        payload = client.compare_commits(repo, before, after)
        commits = [_normalize_commit(item) for item in payload.get("commits", [])]
    else:
        commits = [_normalize_commit(item) for item in event.get("commits", [])]

    head_commit = event.get("head_commit")
    if head_commit:
        normalized_head = _normalize_commit(
            {"sha": after, "message": head_commit.get("message", "")},
        )
        if normalized_head["sha"] and normalized_head["sha"] not in {
            item["sha"] for item in commits
        }:
            commits.append(normalized_head)

    return [item for item in commits if item["sha"]]


def _select_merged_develop_pr(
    commit_sha: str,
    pull_requests: list[dict[str, Any]],
) -> dict[str, Any] | None:
    merged = [
        pr
        for pr in pull_requests
        if pr.get("merged_at") and pr.get("base", {}).get("ref") == "develop"
    ]
    if not merged:
        return None

    exact_merge = [pr for pr in merged if pr.get("merge_commit_sha") == commit_sha]
    if len(exact_merge) == 1:
        return exact_merge[0]

    if len(merged) == 1:
        return merged[0]

    merged.sort(key=lambda item: item.get("merged_at") or "", reverse=True)
    return merged[0]


def _successful_pr_validation_run(
    client: GitHubApiClient,
    repo: str,
    head_sha: str,
) -> dict[str, Any] | None:
    runs = client.workflow_runs_for_head_sha(repo, head_sha)
    successful = [
        run
        for run in runs
        if run.get("name") == "pr-validation"
        and run.get("head_sha") == head_sha
        and run.get("conclusion") == "success"
    ]
    if not successful:
        return None

    successful.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return successful[0]


def _validate_commit(
    client: GitHubApiClient,
    repo: str,
    commit: dict[str, str],
) -> CommitValidation:
    sha = commit["sha"]
    message = commit["message"]
    pull_requests = client.associated_pull_requests(repo, sha)
    selected_pr = _select_merged_develop_pr(sha, pull_requests)
    if selected_pr is None:
        return CommitValidation(
            sha=sha,
            message=message,
            valid=False,
            reason="commit is not associated with a merged develop pull request",
        )

    pr_number = int(selected_pr["number"])
    head_sha = str(selected_pr.get("head", {}).get("sha") or "")
    if not head_sha:
        return CommitValidation(
            sha=sha,
            message=message,
            valid=False,
            reason=f"pull request #{pr_number} is missing head sha metadata",
            pr_number=pr_number,
        )

    validation_run = _successful_pr_validation_run(client, repo, head_sha)
    if validation_run is None:
        return CommitValidation(
            sha=sha,
            message=message,
            valid=False,
            reason=f"pull request #{pr_number} has no successful pr-validation run for head {head_sha}",
            pr_number=pr_number,
            pr_head_sha=head_sha,
        )

    return CommitValidation(
        sha=sha,
        message=message,
        valid=True,
        reason="ok",
        pr_number=pr_number,
        pr_head_sha=head_sha,
        pr_validation_run_id=int(validation_run["id"]),
    )


def evaluate_push_policy(
    client: GitHubApiClient,
    repo: str,
    event: dict[str, Any],
) -> PolicyDecision:
    before = str(event.get("before") or "")
    after = str(event.get("after") or "")
    forced = bool(event.get("forced"))

    if _is_guard_revert_push(event):
        return PolicyDecision(
            action="allow",
            reason="allowing develop-guard remediation push",
            before=before,
            after=after,
            forced=forced,
            pr_number=None,
            invalid_commits=[],
            validations=[],
        )

    if forced:
        return PolicyDecision(
            action="restore_ref",
            reason="forced pushes to develop are not allowed",
            before=before,
            after=after,
            forced=forced,
            pr_number=None,
            invalid_commits=[after] if after else [],
            validations=[],
        )

    commits = _list_push_commits(client, repo, event)
    if not commits:
        return PolicyDecision(
            action="revert_range",
            reason="unable to determine commits introduced on develop push",
            before=before,
            after=after,
            forced=forced,
            pr_number=None,
            invalid_commits=[],
            validations=[],
        )

    validations = [_validate_commit(client, repo, commit) for commit in commits]
    invalid = [item.sha for item in validations if not item.valid]
    if invalid:
        reason = "; ".join(
            f"{item.sha[:12]}: {item.reason}" for item in validations if not item.valid
        )
        return PolicyDecision(
            action="revert_range",
            reason=reason,
            before=before,
            after=after,
            forced=forced,
            pr_number=None,
            invalid_commits=invalid,
            validations=validations,
        )

    pr_numbers = {item.pr_number for item in validations if item.pr_number is not None}
    if len(pr_numbers) != 1:
        return PolicyDecision(
            action="revert_range",
            reason="develop pushes must introduce commits from exactly one merged pull request",
            before=before,
            after=after,
            forced=forced,
            pr_number=None,
            invalid_commits=[item.sha for item in validations],
            validations=validations,
        )

    pr_number = next(iter(pr_numbers))
    return PolicyDecision(
        action="allow",
        reason=f"allowing merged pull request #{pr_number}",
        before=before,
        after=after,
        forced=forced,
        pr_number=pr_number,
        invalid_commits=[],
        validations=validations,
    )


def resolve_output_path(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.name:
        raise ValueError(f"Output path must point to a file, got {raw_path!r}")
    if candidate.is_absolute():
        allowed_roots = [Path.cwd().resolve()]
        allowed_roots.extend(
            Path(os.environ[root_name]).resolve()
            for root_name in ("RUNNER_TEMP", "GITHUB_WORKSPACE")
            if os.environ.get(root_name)
        )
        resolved = candidate.resolve(strict=False)
        if allowed_roots and not any(
            resolved == root or root in resolved.parents for root in allowed_roots
        ):
            raise ValueError(
                f"Absolute output path must remain under workspace or runner temp, got {raw_path!r}",
            )
        return resolved

    return (Path.cwd() / candidate).resolve(strict=False)


def resolve_existing_input_path(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Input path must point to a file, got {raw_path!r}")
    return resolved


def _write_github_outputs(path: str, decision: PolicyDecision) -> None:
    output_path = resolve_output_path(path)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(f"action={decision.action}\n")
        handle.write(f"reason={decision.reason}\n")
        handle.write(f"before={decision.before}\n")
        handle.write(f"after={decision.after}\n")
        handle.write(f"forced={'true' if decision.forced else 'false'}\n")
        handle.write(f"pr_number={decision.pr_number or ''}\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--event-path", required=True, help="Path to the GitHub push event JSON")
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN", ""), help="GitHub token")
    parser.add_argument("--output-json", help="Optional path for decision JSON")
    parser.add_argument("--github-output", help="Optional GitHub Actions output file path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not args.token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 1

    event_path = resolve_existing_input_path(args.event_path)
    with event_path.open(encoding="utf-8") as handle:
        event = json.load(handle)

    try:
        decision = evaluate_push_policy(GitHubApiClient(args.token), args.repo, event)
    except Exception as e:  # pragma: no cover - CLI error handling
        print(f"develop push policy evaluation failed: {e}", file=sys.stderr)
        return 1

    payload = decision.to_dict()
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if args.output_json:
        output_json_path = resolve_output_path(args.output_json)
        output_json_path.parent.mkdir(parents=True, exist_ok=True)
        output_json_path.write_text(f"{serialized}\n", encoding="utf-8")

    if args.github_output:
        _write_github_outputs(args.github_output, decision)

    print(serialized)
    if decision.action == "allow":
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
