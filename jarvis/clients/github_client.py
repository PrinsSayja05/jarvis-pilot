"""GitHub client: GitHub App, or TEMPORARILY a personal access token.

Rules: PRs are always draft=True. JARVIS never merges - this client has
no merge() call anywhere, on purpose.

TEMPORARY (WMCNL-2514): the WAMOCON-JARVIS App is not installed on the Wamocon
organisation yet. Until it is, GITHUB_TOKEN (a personal access token) is used
directly: no JWT, no installation-token exchange. Remove GITHUB_TOKEN from .env
once the App is installed, and the App flow below is used again.
"""
from __future__ import annotations

import logging

from github import Auth, Github, GithubException, GithubIntegration, UnknownObjectException

from jarvis.config import GitHubSettings

logger = logging.getLogger("jarvis.github")
_mode_logged = False


def log_auth_mode(settings: GitHubSettings) -> None:
    """Log once per process which GitHub auth is active (called at CLI / console startup)."""
    global _mode_logged
    if not _mode_logged:
        logger.info("GitHub auth: %s", settings.auth_mode)
        _mode_logged = True


class GitHubClient:
    def __init__(self, settings: GitHubSettings) -> None:
        self._settings = settings
        log_auth_mode(settings)
        # TEMPORARY personal token (WMCNL-2514); otherwise the App's short-lived installation token.
        self._token = settings.token or self._fetch_installation_token()
        self._github = Github(auth=Auth.Token(self._token))

    def _fetch_installation_token(self) -> str:
        with open(self._settings.private_key_path, "r", encoding="utf-8") as f:
            private_key = f.read()

        integration = GithubIntegration(self._settings.app_id, private_key)
        installation = integration.get_installation(
            owner=self._settings.org, repo=self._settings.pilot_repo
        )
        return integration.get_access_token(installation.id).token

    @property
    def token(self) -> str:
        return self._token

    def get_repo(self, full_name: str):
        return self._github.get_repo(full_name)

    def find_repo_for_project(self, project_key: str) -> str:
        """Match a Jira project key to a repo. V0: falls back to the configured pilot repo."""
        org = self._github.get_organization(self._settings.org)
        for repo in org.get_repos():
            if project_key.lower() in repo.name.lower():
                return repo.full_name
        return f"{self._settings.org}/{self._settings.pilot_repo}"

    def push_branch_with_files(
        self,
        *,
        repo_full_name: str,
        branch: str,
        base_branch: str,
        files: dict[str, str],
        commit_message: str,
    ) -> None:
        repo = self._github.get_repo(repo_full_name)
        base_sha = repo.get_git_ref(f"heads/{base_branch}").object.sha

        try:
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
        except GithubException as exc:
            if exc.status != 422:  # 422 = ref already exists, fine for a repair re-run
                raise

        for path, content in files.items():
            try:
                existing = repo.get_contents(path, ref=branch)
                repo.update_file(path, commit_message, content, existing.sha, branch=branch)
            except UnknownObjectException:
                repo.create_file(path, commit_message, content, branch=branch)

    def open_draft_pr(
        self,
        *,
        repo_full_name: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        repo = self._github.get_repo(repo_full_name)
        # Re-runs push to the same branch; reuse its open PR instead of failing on a duplicate.
        owner = repo_full_name.split("/")[0]
        for existing in repo.get_pulls(state="open", head=f"{owner}:{branch}"):
            return existing.html_url
        pull_request = repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base=base_branch,
            draft=True,  # Rule: PR is always draft=True - never set draft=False.
        )
        return pull_request.html_url
