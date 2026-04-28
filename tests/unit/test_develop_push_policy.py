from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "ci" / "enforce_develop_push_policy.py"
)
SPEC = importlib.util.spec_from_file_location("develop_push_policy", MODULE_PATH)
if SPEC is None:
    raise RuntimeError(f"Unable to create import spec for {MODULE_PATH}")
if SPEC.loader is None:
    raise RuntimeError(f"Import spec for {MODULE_PATH} is missing a loader")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeGitHubApiClient:
    def __init__(self, *, compare_commits, pull_requests_by_sha, runs_by_head_sha):
        self._compare_commits = compare_commits
        self._pull_requests_by_sha = pull_requests_by_sha
        self._runs_by_head_sha = runs_by_head_sha

    def compare_commits(self, repo: str, before: str, after: str):
        _expect(repo == "antonga23/cloudbet-market-maker", f"unexpected repo: {repo}")
        _expect(before == "before", f"unexpected before sha: {before}")
        return {"commits": self._compare_commits}

    def associated_pull_requests(self, repo: str, commit_sha: str):
        _expect(repo == "antonga23/cloudbet-market-maker", f"unexpected repo: {repo}")
        return self._pull_requests_by_sha.get(commit_sha, [])

    def workflow_runs_for_head_sha(self, repo: str, head_sha: str):
        _expect(repo == "antonga23/cloudbet-market-maker", f"unexpected repo: {repo}")
        return self._runs_by_head_sha.get(head_sha, [])


def _push_event(
    *,
    after: str = "after",
    forced: bool = False,
    sender: str = "antonga23",
    head_message: str = "merge",
):
    return {
        "before": "before",
        "after": after,
        "forced": forced,
        "sender": {"login": sender},
        "head_commit": {"message": head_message},
    }


def _merged_pr(number: int, head_sha: str, *, merge_commit_sha: str | None = None):
    return {
        "number": number,
        "merged_at": "2026-04-28T15:27:27Z",
        "merge_commit_sha": merge_commit_sha,
        "base": {"ref": "develop"},
        "head": {"sha": head_sha},
    }


def _validation_run(run_id: int, head_sha: str):
    return {
        "id": run_id,
        "name": "pr-validation",
        "head_sha": head_sha,
        "conclusion": "success",
        "created_at": "2026-04-28T13:00:00Z",
    }


def test_evaluate_push_policy_allows_green_pr_merge():
    client = FakeGitHubApiClient(
        compare_commits=[{"sha": "after", "commit": {"message": "Merge pull request #55"}}],
        pull_requests_by_sha={"after": [_merged_pr(55, "pr-head", merge_commit_sha="after")]},
        runs_by_head_sha={"pr-head": [_validation_run(25049770395, "pr-head")]},
    )

    decision = MODULE.evaluate_push_policy(
        client,
        "antonga23/cloudbet-market-maker",
        _push_event(head_message="Merge pull request #55"),
    )

    _expect(decision.action == "allow", f"unexpected action: {decision.action}")
    _expect(decision.pr_number == 55, f"unexpected pr_number: {decision.pr_number}")
    _expect(
        decision.invalid_commits == [],
        f"unexpected invalid commits: {decision.invalid_commits}",
    )
    _expect(len(decision.validations) == 1, f"unexpected validations: {decision.validations}")
    _expect(
        decision.validations[0].pr_validation_run_id == 25049770395,
        f"unexpected run id: {decision.validations[0].pr_validation_run_id}",
    )


def test_evaluate_push_policy_reverts_direct_push_without_pr():
    client = FakeGitHubApiClient(
        compare_commits=[{"sha": "after", "commit": {"message": "hotfix directly on develop"}}],
        pull_requests_by_sha={},
        runs_by_head_sha={},
    )

    decision = MODULE.evaluate_push_policy(
        client,
        "antonga23/cloudbet-market-maker",
        _push_event(head_message="hotfix directly on develop"),
    )

    _expect(decision.action == "revert_range", f"unexpected action: {decision.action}")
    _expect(
        decision.invalid_commits == ["after"],
        f"unexpected invalid commits: {decision.invalid_commits}",
    )
    _expect(
        "not associated with a merged develop pull request" in decision.reason,
        f"unexpected reason: {decision.reason}",
    )


def test_evaluate_push_policy_reverts_pr_without_successful_validation():
    client = FakeGitHubApiClient(
        compare_commits=[{"sha": "after", "commit": {"message": "Merge pull request #99"}}],
        pull_requests_by_sha={"after": [_merged_pr(99, "pr-head", merge_commit_sha="after")]},
        runs_by_head_sha={"pr-head": []},
    )

    decision = MODULE.evaluate_push_policy(
        client,
        "antonga23/cloudbet-market-maker",
        _push_event(head_message="Merge pull request #99"),
    )

    _expect(decision.action == "revert_range", f"unexpected action: {decision.action}")
    _expect(
        decision.invalid_commits == ["after"],
        f"unexpected invalid commits: {decision.invalid_commits}",
    )
    _expect(
        "has no successful pr-validation run" in decision.reason,
        f"unexpected reason: {decision.reason}",
    )


def test_evaluate_push_policy_restores_forced_pushes():
    client = FakeGitHubApiClient(compare_commits=[], pull_requests_by_sha={}, runs_by_head_sha={})

    decision = MODULE.evaluate_push_policy(
        client,
        "antonga23/cloudbet-market-maker",
        _push_event(forced=True, head_message="force update"),
    )

    _expect(decision.action == "restore_ref", f"unexpected action: {decision.action}")
    _expect(
        decision.invalid_commits == ["after"],
        f"unexpected invalid commits: {decision.invalid_commits}",
    )
    _expect(
        "forced pushes to develop" in decision.reason,
        f"unexpected reason: {decision.reason}",
    )


def test_evaluate_push_policy_allows_guard_revert_push():
    client = FakeGitHubApiClient(compare_commits=[], pull_requests_by_sha={}, runs_by_head_sha={})

    decision = MODULE.evaluate_push_policy(
        client,
        "antonga23/cloudbet-market-maker",
        _push_event(
            sender="github-actions[bot]",
            head_message="[develop-guard] Revert unauthorized develop push after",
        ),
    )

    _expect(decision.action == "allow", f"unexpected action: {decision.action}")
    _expect(
        decision.reason == "allowing develop-guard remediation push",
        f"unexpected reason: {decision.reason}",
    )
    _expect(decision.validations == [], f"unexpected validations: {decision.validations}")


def test_evaluate_push_policy_reverts_pushes_with_multiple_prs():
    client = FakeGitHubApiClient(
        compare_commits=[
            {"sha": "commit-one", "commit": {"message": "commit one"}},
            {"sha": "commit-two", "commit": {"message": "commit two"}},
        ],
        pull_requests_by_sha={
            "commit-one": [_merged_pr(10, "head-one")],
            "commit-two": [_merged_pr(11, "head-two")],
        },
        runs_by_head_sha={
            "head-one": [_validation_run(1, "head-one")],
            "head-two": [_validation_run(2, "head-two")],
        },
    )

    decision = MODULE.evaluate_push_policy(
        client,
        "antonga23/cloudbet-market-maker",
        _push_event(after="commit-two", head_message="multiple commits"),
    )

    _expect(decision.action == "revert_range", f"unexpected action: {decision.action}")
    _expect(
        set(decision.invalid_commits) == {"commit-one", "commit-two"},
        f"unexpected invalid commits: {decision.invalid_commits}",
    )
    _expect(
        "exactly one merged pull request" in decision.reason,
        f"unexpected reason: {decision.reason}",
    )
