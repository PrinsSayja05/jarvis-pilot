"""Jira REST API client."""
from __future__ import annotations

import re

import httpx

from jarvis.config import JiraSettings
from jarvis.models.ticket import JiraTicket, TicketComment

# Acceptance criteria are custom fields whose ids differ per Jira site; match them by name.
_ACCEPTANCE_FIELD_NAMES = ("acceptance criteria", "akzeptanzkriterien")
# Comments JARVIS wrote itself are not context for the planner.
JARVIS_COMMENT_PREFIXES = ("🤖 JARVIS Plan Ready", "JARVIS hat einen Draft PR", "JARVIS could not complete")


class JiraClient:
    def __init__(self, settings: JiraSettings) -> None:
        self._base_url = settings.base_url.rstrip("/")
        self._auth = (settings.email, settings.api_token)

    def get_ticket(self, ticket_id: str) -> JiraTicket:
        """The full ticket: description, acceptance criteria, labels and human comments."""
        url = f"{self._base_url}/rest/api/3/issue/{ticket_id}"
        with httpx.Client(auth=self._auth, timeout=30) as client:
            acceptance_ids = self._acceptance_field_ids(client)
            response = client.get(
                url,
                params={"fields": ",".join(["summary", "description", "issuetype", "status", "labels", "comment",
                                            *acceptance_ids])},
            )
        response.raise_for_status()
        data = response.json()
        fields = data["fields"]

        acceptance = next(
            (text for text in (_extract_description(fields.get(fid)) for fid in acceptance_ids) if text.strip()), ""
        )
        comments = [
            TicketComment(
                author=c.get("author", {}).get("displayName", "?"),
                created=c.get("created", "")[:10],
                body=_extract_description(c.get("body")),
            )
            for c in (fields.get("comment") or {}).get("comments", [])
        ]
        return JiraTicket(
            key=data["key"],
            summary=fields.get("summary", ""),
            description=_extract_description(fields.get("description")),
            issue_type=fields.get("issuetype", {}).get("name", ""),
            status=fields.get("status", {}).get("name", ""),
            labels=fields.get("labels", []),
            url=f"{self._base_url}/browse/{data['key']}",
            acceptance_criteria=acceptance,
            comments=[c for c in comments if c.body.strip() and not c.body.startswith(JARVIS_COMMENT_PREFIXES)],
        )

    def _acceptance_field_ids(self, client: httpx.Client) -> list[str]:
        response = client.get(f"{self._base_url}/rest/api/3/field")
        response.raise_for_status()
        return [
            f["id"]
            for f in response.json()
            if f.get("custom") and f.get("schema", {}).get("type") == "string"
            and f.get("name", "").lower().startswith(_ACCEPTANCE_FIELD_NAMES)
        ]

    def search_open(self, project: str, limit: int = 50) -> list[JiraTicket]:
        """Open (not Done) tickets of a project, newest first. `project` must be a validated key."""
        jql = f"project = {project} AND statusCategory != Done ORDER BY created DESC"
        with httpx.Client(auth=self._auth, timeout=30) as client:
            response = client.get(
                f"{self._base_url}/rest/api/3/search/jql",
                params={"jql": jql, "maxResults": limit, "fields": "summary,issuetype,status"},
            )
        response.raise_for_status()
        return [
            JiraTicket(
                key=issue["key"],
                summary=issue["fields"].get("summary", ""),
                description="",
                issue_type=issue["fields"].get("issuetype", {}).get("name", ""),
                status=issue["fields"].get("status", {}).get("name", ""),
                url=f"{self._base_url}/browse/{issue['key']}",
            )
            for issue in response.json().get("issues", [])
        ]

    def create_issue(self, project: str, summary: str, description: str, issue_type: str = "Task") -> JiraTicket:
        paragraphs = [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in description.splitlines()
            if line.strip()
        ]
        body = {
            "fields": {
                "project": {"key": project},
                "issuetype": {"name": issue_type},
                "summary": summary,
                "description": {"type": "doc", "version": 1, "content": paragraphs},
            }
        }
        if not paragraphs:
            del body["fields"]["description"]
        with httpx.Client(auth=self._auth, timeout=30) as client:
            response = client.post(f"{self._base_url}/rest/api/3/issue", json=body)
        response.raise_for_status()
        key = response.json()["key"]
        return JiraTicket(
            key=key, summary=summary, description=description, issue_type=issue_type,
            status="", url=f"{self._base_url}/browse/{key}",
        )

    def add_comment(self, issue_key: str, text: str) -> None:
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/comment"
        # One paragraph per line: a "\n" inside a single ADF text node is not shown as a line break.
        paragraphs = [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line.strip() else {"type": "paragraph"}
            for line in text.splitlines()
        ]
        body = {"body": {"type": "doc", "version": 1, "content": paragraphs}}
        with httpx.Client(auth=self._auth, timeout=30) as client:
            response = client.post(url, json=body)
        response.raise_for_status()

    def add_attachment(self, issue_key: str, filename: str, content: bytes) -> None:
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/attachments"
        with httpx.Client(auth=self._auth, timeout=60) as client:
            response = client.post(
                url,
                headers={"X-Atlassian-Token": "no-check"},  # required by Jira for attachment uploads
                files={"file": (filename, content, "text/x-diff")},
            )
        response.raise_for_status()

    def transition_to_next(self, issue_key: str) -> None:
        """Move the issue to the next workflow status (e.g. after opening a PR)."""
        transitions_url = f"{self._base_url}/rest/api/3/issue/{issue_key}/transitions"
        with httpx.Client(auth=self._auth, timeout=30) as client:
            response = client.get(transitions_url)
            response.raise_for_status()
            transitions = response.json()["transitions"]
            if not transitions:
                return
            next_transition = transitions[0]
            client.post(transitions_url, json={"transition": {"id": next_transition["id"]}})


def _extract_description(description_field: dict | str | None) -> str:
    """Jira Cloud returns description as Atlassian Document Format (ADF)."""
    if description_field is None:
        return ""
    if isinstance(description_field, str):
        return description_field

    parts: list[str] = []

    def _walk(node: dict) -> None:
        node_type = node.get("type")
        if node_type == "text":
            parts.append(node.get("text", ""))
        elif node_type == "hardBreak":
            parts.append("\n")
        elif node_type == "listItem":
            parts.append("- ")
        for child in node.get("content", []):
            _walk(child)
        if node_type in _BLOCK_NODES:  # keep the ticket's line structure for the planner
            parts.append("\n")

    _walk(description_field)
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


_BLOCK_NODES = {"paragraph", "heading", "codeBlock", "blockquote", "rule", "tableRow"}
