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
# Comments JARVIS wrote itself are not context for the planner (current formats and the ones before 30.09.2026).
JARVIS_COMMENT_PREFIXES = (
    "J.A.R.V.I.S.",  # the format used since 30.09.2026, in every icon variant
    "🧭 JARVIS", "🔗 JARVIS", "❌ JARVIS", "✅ JARVIS",
    "🤖 JARVIS Plan Ready", "JARVIS hat einen Draft PR", "JARVIS could not complete",
)
# Set while a full run works on the ticket; removed when the run ends, whatever the outcome.
IN_PROGRESS_LABEL = "jarvis-in-progress"
PANEL_TYPES = ("info", "note", "success", "warning", "error")


def is_jarvis_comment(body: str) -> bool:
    """First line carries a JARVIS marker; it may follow an @mention (batch summaries mention the assignee)."""
    first = body.lstrip().split("\n", 1)[0]
    return any(prefix in first for prefix in JARVIS_COMMENT_PREFIXES)


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
                params={"fields": ",".join(["summary", "description", "issuetype", "status", "labels", "comment", "priority", "assignee",
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
            priority=(fields.get("priority") or {}).get("name", ""),
            assignee_id=(fields.get("assignee") or {}).get("accountId", ""),
            assignee_name=(fields.get("assignee") or {}).get("displayName", ""),
            comments=[c for c in comments if c.body.strip() and not is_jarvis_comment(c.body)],
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

    def search_open(self, project: str, limit: int = 50, assignee_id: str | None = None) -> list[JiraTicket]:
        """Open (not Done) tickets of a project, newest first. `project` and `assignee_id` must be validated."""
        who = f' AND assignee = "{assignee_id}"' if assignee_id else ""
        # Board rank first; callers then sort by priority (stable, so rank decides within one priority).
        jql = f"project = {project}{who} AND statusCategory != Done ORDER BY Rank ASC, created DESC"
        with httpx.Client(auth=self._auth, timeout=30) as client:
            response = client.get(
                f"{self._base_url}/rest/api/3/search/jql",
                params={"jql": jql, "maxResults": limit, "fields": "summary,issuetype,status,labels,assignee,priority"},
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
                assignee_id=(issue["fields"].get("assignee") or {}).get("accountId", ""),
                assignee_name=(issue["fields"].get("assignee") or {}).get("displayName", ""),
                priority=(issue["fields"].get("priority") or {}).get("name", ""),
            )
            for issue in response.json().get("issues", [])
        ]

    def display_name(self, account_id: str) -> str:
        """Name of a Jira account. Needed for people the console knows by id only, such as an
        admin who has no open tickets and therefore appears in no ticket list."""
        with httpx.Client(auth=self._auth, timeout=15) as client:
            response = client.get(f"{self._base_url}/rest/api/3/user", params={"accountId": account_id})
        if response.status_code != 200:
            return ""
        return response.json().get("displayName", "")

    def find_account_by_email(self, email: str) -> tuple[str, str] | None:
        """(accountId, displayName) of the Jira user with this e-mail. Jira finds users by e-mail even when
        their profile hides the address, so this is the reliable link from a Keycloak login to Jira."""
        with httpx.Client(auth=self._auth, timeout=15) as client:
            response = client.get(f"{self._base_url}/rest/api/3/user/search", params={"query": email})
        response.raise_for_status()
        users = [u for u in response.json() if u.get("accountType") == "atlassian" and u.get("active", True)]
        exact = [u for u in users if (u.get("emailAddress") or "").lower() == email.lower()]
        match = exact or (users if len(users) == 1 else [])
        return (match[0]["accountId"], match[0].get("displayName", "")) if match else None

    def assignees_of(self, keys: list[str]) -> dict[str, str]:
        """{ticket key: assignee accountId} for the given (validated) keys, open or closed."""
        if not keys:
            return {}
        with httpx.Client(auth=self._auth, timeout=30) as client:
            response = client.post(f"{self._base_url}/rest/api/3/search/jql",
                                   json={"jql": f"key in ({', '.join(keys)})", "fields": ["assignee"], "maxResults": len(keys)})
        response.raise_for_status()
        return {i["key"]: (i["fields"].get("assignee") or {}).get("accountId", "") for i in response.json().get("issues", [])}

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

    def add_comment(self, issue_key: str, text: str, *, panel: str | None = None, mention: str | None = None) -> None:
        """`panel` (info, success, warning, error, note) frames the comment in a coloured Jira panel;
        `mention` is an accountId put in front of the first line, which makes Jira notify that person."""
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/comment"
        # One paragraph per line: a "\n" inside a single ADF text node is not shown as a line break.
        paragraphs = [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line.strip() else {"type": "paragraph"}
            for line in text.splitlines()
        ]
        if mention and paragraphs:
            paragraphs[0].setdefault("content", []).insert(0, {"type": "mention", "attrs": {"id": mention}})
            paragraphs[0]["content"].insert(1, {"type": "text", "text": " "})
        if panel in PANEL_TYPES:
            paragraphs = [{"type": "panel", "attrs": {"panelType": panel}, "content": paragraphs}]
        body = {"body": {"type": "doc", "version": 1, "content": paragraphs}}
        with httpx.Client(auth=self._auth, timeout=30) as client:
            response = client.post(url, json=body)
        response.raise_for_status()

    def set_label(self, issue_key: str, label: str, present: bool) -> None:
        """Add or remove one label without touching the others (idempotent both ways)."""
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}"
        body = {"update": {"labels": [{"add" if present else "remove": label}]}}
        with httpx.Client(auth=self._auth, timeout=15) as client:
            # A label is a marker, not news: no e-mail for it. Suppressing mails needs project admin rights,
            # so without them (403) the change is made with Jira's default notification.
            response = client.put(url, params={"notifyUsers": "false"}, json=body)
            if response.status_code == 403:
                response = client.put(url, json=body)
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
        elif node_type == "mention":
            parts.append(node.get("attrs", {}).get("text", "@"))
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
