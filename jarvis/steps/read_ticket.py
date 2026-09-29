from __future__ import annotations

from jarvis.clients.jira_client import JiraClient
from jarvis.config import JarvisConfig
from jarvis.models.ticket import JiraTicket


def read_ticket(ticket_id: str, config: JarvisConfig) -> JiraTicket:
    client = JiraClient(config.jira)
    return client.get_ticket(ticket_id)
