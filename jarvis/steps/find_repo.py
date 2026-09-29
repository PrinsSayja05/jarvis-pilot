from __future__ import annotations

from jarvis.config import JarvisConfig
from jarvis.models.ticket import JiraTicket


def find_repo(ticket: JiraTicket, config: JarvisConfig) -> str:
    # V0: only one pilot repo exists. Real ticket-to-repo matching comes later.
    return f"{config.github.org}/{config.github.pilot_repo}"
