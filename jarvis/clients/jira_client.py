"""Jira REST API client."""
from __future__ import annotations

import logging
import re

import httpx

from jarvis.config import JiraSettings
from jarvis.models.ticket import JiraTicket, TicketComment

logger = logging.getLogger("jarvis.jira")

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
                params={"jql": jql, "maxResults": limit, "fields": "summary,issuetype,status,labels"},
            )
        response.raise_for_status()
        return [
            JiraTicket(
                key=issue["key"],
                summary=issue["fields"].get("summary", ""),
                description="",
                issue_type=issue["fields"].get("issuetype", {}).get("name", ""),
                status=issue["fields"].get("status", {}).get("name", ""),
                labels=issue["fields"].get("labels", []),
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

    def transition_to_review(self, issue_key: str) -> str | None:
        """After a PR: move the issue to its review status. Returns the new status, or None if skipped.

        Workflows differ per project (WMCNL has "Review/Test", JW has no review status at all),
        so the target is matched by name. Never moves an issue that is already done.
        """
        transitions_url = f"{self._base_url}/rest/api/3/issue/{issue_key}/transitions"
        with httpx.Client(auth=self._auth, timeout=30) as client:
            issue = client.get(f"{self._base_url}/rest/api/3/issue/{issue_key}", params={"fields": "status"})
            issue.raise_for_status()
            status = issue.json()["fields"]["status"]
            response = client.get(transitions_url)
            response.raise_for_status()
            transitions = response.json()["transitions"]

            if status.get("statusCategory", {}).get("key") == "done":
                logger.info("jira status %s: already done (%s), not moved", issue_key, status["name"])
                return None
            chosen = pick_review_transition(transitions)
            if chosen is None:
                logger.warning(
                    "jira status %s: no review status in this workflow, staying in %r. Available transitions: %s",
                    issue_key, status["name"], ", ".join(f"{t['name']} -> {t['to']['name']}" for t in transitions),
                )
                return None
            if chosen["to"]["name"].casefold() == status["name"].casefold():
                return None
            client.post(transitions_url, json={"transition": {"id": chosen["id"]}}).raise_for_status()
            logger.info("jira status %s: %s -> %s", issue_key, status["name"], chosen["to"]["name"])
            return chosen["to"]["name"]


# Best match first: exact review statuses, then anything that reads like review.
_REVIEW_EXACT = ("in review", "in prüfung", "review", "review/test", "code review")
_REVIEW_PARTS = ("review", "prüfung", "pruefung")


def pick_review_transition(transitions: list[dict]) -> dict | None:
    """The transition whose target status is the review status, matched by the target's name."""
    def target(t: dict) -> str:
        return t.get("to", {}).get("name", "").strip().casefold()

    for wanted in _REVIEW_EXACT:
        for t in transitions:
            if target(t) == wanted:
                return t
    for t in transitions:
        if any(part in target(t) for part in _REVIEW_PARTS):
            return t
    return None


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
