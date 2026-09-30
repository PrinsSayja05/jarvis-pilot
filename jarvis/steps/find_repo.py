"""Pick the GitHub repository a ticket belongs to.

A ticket names its repository with a Jira label "repo:<name>" (e.g. repo:wmc-rechnungsservice),
which resolves to GITHUB_ORG/<name>. Without such a label JARVIS falls back to GITHUB_PILOT_REPO.
"""
from __future__ import annotations

import logging
import re

from jarvis.config import JarvisConfig
from jarvis.models.ticket import JiraTicket

logger = logging.getLogger("jarvis.find_repo")

_LABEL_PREFIX = "repo:"
# GitHub repository names: letters, digits, '.', '-', '_'. Nothing that could reach another owner or a path.
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class RepoLabelError(ValueError):
    """The ticket's repo label is invalid or ambiguous."""


def choose_repo(ticket: JiraTicket, config: JarvisConfig) -> tuple[str, str]:
    """Returns (owner/name, reason)."""
    names = sorted({label[len(_LABEL_PREFIX):] for label in ticket.labels if label.startswith(_LABEL_PREFIX)})
    if len(names) > 1:
        raise RepoLabelError(f"{ticket.key} has several repo labels ({', '.join(names)}); keep exactly one")
    if names:
        name = names[0]
        if name in (".", "..") or not _REPO_NAME.match(name):
            raise RepoLabelError(f"{ticket.key} has an invalid repo label {_LABEL_PREFIX}{name!r}")
        return f"{config.github.org}/{name}", f"label {_LABEL_PREFIX}{name}"
    return f"{config.github.org}/{config.github.pilot_repo}", "no repo: label, default GITHUB_PILOT_REPO"


def find_repo(ticket: JiraTicket, config: JarvisConfig) -> str:
    repo, reason = choose_repo(ticket, config)
    logger.info("find_repo ticket=%s repo=%s reason=%s", ticket.key, repo, reason)
    return repo
