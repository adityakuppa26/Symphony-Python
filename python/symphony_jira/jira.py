from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Mapping

import httpx

from .config import TrackerConfig, resolve_configured_secret
from .models import Issue, IssueBlocker, IssueComment


class JiraError(Exception):
    """Base Jira adapter error."""


class JiraAuthError(JiraError):
    """Raised when Jira credentials cannot be resolved."""


class JiraClient:
    def __init__(
        self,
        config: TrackerConfig,
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.config = config
        self.environ = environ or os.environ
        headers, auth = self._auth()
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            auth=auth,
            transport=transport,
            timeout=timeout,
        )

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def search_issues(self, jql: str, limit: int) -> list[Issue]:
        response = await self._client.get(
            "/rest/api/2/search",
            params={
                "jql": jql,
                "maxResults": limit,
                "fields": "summary,description,status,priority,issuetype,assignee,reporter,labels,created,updated,comment,issuelinks",
            },
        )
        response.raise_for_status()
        payload = response.json()
        return [normalize_issue(issue, self.config.base_url) for issue in payload.get("issues", [])]

    async def get_issue(self, key: str, include_comments: bool = True) -> Issue:
        fields = "summary,description,status,priority,issuetype,assignee,reporter,labels,created,updated,issuelinks"
        if include_comments:
            fields += ",comment"
        response = await self._client.get(f"/rest/api/2/issue/{key}", params={"fields": fields})
        response.raise_for_status()
        return normalize_issue(response.json(), self.config.base_url)

    async def add_comment(self, key: str, body: str) -> None:
        response = await self._client.post(f"/rest/api/2/issue/{key}/comment", json={"body": body})
        response.raise_for_status()

    async def transition_issue(self, key: str, target_status: str) -> bool:
        response = await self._client.get(f"/rest/api/2/issue/{key}/transitions")
        response.raise_for_status()
        transitions = response.json().get("transitions", [])
        wanted = target_status.lower()
        chosen: str | None = None
        for transition in transitions:
            name = str(transition.get("name", "")).lower()
            to_name = str((transition.get("to") or {}).get("name", "")).lower()
            if wanted in {name, to_name}:
                chosen = str(transition["id"])
                break
        if not chosen:
            return False
        post = await self._client.post(f"/rest/api/2/issue/{key}/transitions", json={"transition": {"id": chosen}})
        post.raise_for_status()
        return True

    def _auth(self) -> tuple[dict[str, str], httpx.Auth | None]:
        token = resolve_configured_secret(
            env_name=self.config.auth.token_env,
            config_file=self.config.auth.token_config_file,
            config_key=self.config.auth.token_config_key,
            environ=self.environ,
        )
        if not token:
            raise JiraAuthError(f"Jira token is not configured via {self.config.auth.token_env} or token_config_file")

        if self.config.auth.mode == "basic":
            email_env = self.config.auth.email_env
            email = resolve_configured_secret(
                env_name=email_env,
                config_file=self.config.auth.token_config_file,
                config_key=self.config.auth.email_config_key,
                environ=self.environ,
            )
            if not email:
                raise JiraAuthError(f"Jira email is not configured via {email_env} or email_config_key")
            return {}, httpx.BasicAuth(email, token)
        return {"Authorization": f"Bearer {token}"}, None


def normalize_issue(payload: Mapping[str, Any], base_url: str) -> Issue:
    fields = payload.get("fields") or {}
    status = fields.get("status") or {}
    priority = fields.get("priority") or {}
    issue_type = fields.get("issuetype") or {}
    assignee = fields.get("assignee") or {}
    reporter = fields.get("reporter") or {}
    comments_payload = ((fields.get("comment") or {}).get("comments") or [])

    key = str(payload.get("key") or "")
    labels = [str(label).lower() for label in fields.get("labels") or []]
    comments = [normalize_comment(comment) for comment in comments_payload]

    return Issue(
        id=str(payload.get("id") or key),
        identifier=key,
        title=str(fields.get("summary") or ""),
        description=text_from_jira_value(fields.get("description")),
        status=str(status.get("name") or ""),
        priority=priority.get("name"),
        issue_type=issue_type.get("name"),
        assignee=display_name(assignee),
        reporter=display_name(reporter),
        labels=labels,
        url=f"{base_url.rstrip('/')}/browse/{key}" if base_url else "",
        created_at=parse_jira_datetime(fields.get("created")),
        updated_at=parse_jira_datetime(fields.get("updated")),
        comments=comments,
        blocked_by=normalize_blockers(fields.get("issuelinks") or []),
        raw=dict(payload),
    )


def normalize_comment(payload: Mapping[str, Any]) -> IssueComment:
    return IssueComment(
        id=str(payload.get("id")) if payload.get("id") is not None else None,
        author=display_name(payload.get("author") or payload.get("updateAuthor") or {}),
        body=text_from_jira_value(payload.get("body")),
        created=parse_jira_datetime(payload.get("created")),
    )


def normalize_blockers(links: list[Mapping[str, Any]]) -> list[IssueBlocker]:
    blockers: list[IssueBlocker] = []
    for link in links:
        link_type = (link.get("type") or {})
        inward = str(link_type.get("inward") or "").lower()
        outward = str(link_type.get("outward") or "").lower()
        issue_payload = None
        if "blocked by" in inward and link.get("inwardIssue"):
            issue_payload = link.get("inwardIssue")
        elif "blocks" in outward and link.get("outwardIssue"):
            issue_payload = link.get("outwardIssue")
        if issue_payload:
            fields = issue_payload.get("fields") or {}
            blockers.append(
                IssueBlocker(
                    id=str(issue_payload.get("id")) if issue_payload.get("id") is not None else None,
                    identifier=issue_payload.get("key"),
                    status=(fields.get("status") or {}).get("name"),
                )
            )
    return blockers


def display_name(user: Mapping[str, Any]) -> str | None:
    return user.get("displayName") or user.get("emailAddress") or user.get("name")


def parse_jira_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    text = text.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def text_from_jira_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [text_from_jira_value(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if value.get("type") == "text" and "text" in value:
            return str(value["text"])
        if "content" in value:
            return text_from_jira_value(value["content"])
        if "value" in value:
            return text_from_jira_value(value["value"])
        return json.dumps(value, sort_keys=True)
    return str(value)
