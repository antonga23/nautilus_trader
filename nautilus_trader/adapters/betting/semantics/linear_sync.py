# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Direct Linear GraphQL helpers for semantic mining jobs.
"""

from __future__ import annotations

import json
import os
import ssl
from typing import Any
from urllib import request


LINEAR_API_URL = "https://api.linear.app/graphql"


class LinearSyncError(RuntimeError):
    pass


class LinearIssueSync:
    """
    Minimal Linear GraphQL client aligned to the repo's existing LINEAR_* env contract.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        project_id: str | None = None,
        team_id: str | None = None,
        team_key: str | None = None,
    ) -> None:
        self.api_key = self._clean(api_key or os.getenv("LINEAR_API_KEY"))
        self.project_id = self._clean(project_id or os.getenv("LINEAR_PROJECT_ID"))
        self.team_id = self._clean(team_id or os.getenv("LINEAR_TEAM_ID"))
        self.team_key = self._clean(team_key or os.getenv("LINEAR_TEAM_KEY"))

    @staticmethod
    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and (self.team_id or self.team_key))

    def resolve_team_id(self) -> str:
        if self.team_id:
            return self.team_id
        if not self.team_key:
            raise LinearSyncError("LINEAR_TEAM_ID or LINEAR_TEAM_KEY is required")

        query = """
        query Teams {
          teams {
            nodes { id key name }
          }
        }
        """
        data = self.graphql(query, {})
        for team in data["teams"]["nodes"]:
            if team["key"] == self.team_key:
                self.team_id = team["id"]
                return self.team_id
        raise LinearSyncError(f"Unable to resolve Linear team for key {self.team_key}")

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise LinearSyncError("LINEAR_API_KEY is not configured")

        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = request.Request(  # noqa: S310
            LINEAR_API_URL,
            data=payload,
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        cafile: str | None = None
        try:
            import certifi  # type: ignore
        except ModuleNotFoundError:
            certifi = None
        if certifi is not None:
            cafile = certifi.where()
        elif os.path.exists("/etc/ssl/cert.pem"):
            cafile = "/etc/ssl/cert.pem"
        context = (
            ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        )

        with request.urlopen(req, timeout=30, context=context) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
        if body.get("errors"):
            raise LinearSyncError(str(body["errors"]))
        return body["data"]

    def create_issue(
        self,
        *,
        title: str,
        description: str,
        parent_id: str | None = None,
    ) -> str:
        team_id = self.resolve_team_id()

        query = """
        mutation CreateIssue($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue { id }
          }
        }
        """
        issue_input: dict[str, Any] = {
            "teamId": team_id,
            "title": title,
            "description": description,
        }
        if self.project_id is not None:
            issue_input["projectId"] = self.project_id
        if parent_id is not None:
            issue_input["parentId"] = parent_id
        data = self.graphql(query, {"input": issue_input})
        issue = data["issueCreate"]["issue"]
        if not issue or not issue.get("id"):
            raise LinearSyncError("Linear did not return an issue id")
        return issue["id"]

    def create_comment(self, *, issue_id: str, body: str) -> str:
        query = """
        mutation CreateComment($input: CommentCreateInput!) {
          commentCreate(input: $input) {
            success
            comment { id }
          }
        }
        """
        data = self.graphql(query, {"input": {"issueId": issue_id, "body": body}})
        comment = data["commentCreate"]["comment"]
        if not comment or not comment.get("id"):
            raise LinearSyncError("Linear did not return a comment id")
        return comment["id"]

    def create_semantic_rule_ticket_set(self) -> dict[str, str]:
        if not self.is_configured:
            raise LinearSyncError("Linear sync is not configured")

        parent_id = self.create_issue(
            title="Replace betting market mapper with persisted semantic rule mining",
            description=(
                "Cloudbet-backed semantic corpus refresh, provider normalization, "
                "promotion gating, and runtime matcher integration."
            ),
        )
        subtasks = {
            "normalization": "semantic normalization/payoff vectors",
            "integration": "matcher/graph integration",
            "promotion": "persistence/promotion",
            "sxbet_validation": "SXBET validation",
            "tests_docs_cloud": "tests/docs/cloud validation",
        }
        created: dict[str, str] = {"parent": parent_id}
        for key, title in subtasks.items():
            created[key] = self.create_issue(
                title=title,
                description=f"Subtask of {parent_id}",
                parent_id=parent_id,
            )
        return created
