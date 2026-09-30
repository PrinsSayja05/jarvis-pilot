"""Configuration loading for JARVIS: jarvis.yaml + environment variables.

All secrets come from environment variables (loaded from .env) - never
hardcoded here or in jarvis.yaml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass
class SandboxConfig:
    image: str
    test_command: str
    timeout_seconds: int
    network: str
    url: str


@dataclass
class ModelsConfig:
    coder: str
    planner: str
    reviewer: str
    embedder: str


@dataclass
class LimitsConfig:
    max_steps: int
    max_repair_loops: int
    max_runtime_minutes: int


@dataclass
class GitConfig:
    base_branch: str
    branch_prefix: str
    pr_draft: bool
    auto_merge: bool


@dataclass
class JiraSettings:
    base_url: str
    email: str
    api_token: str


@dataclass
class GitHubSettings:
    app_id: str
    private_key_path: str
    org: str
    pilot_repo: str
    # TEMPORARY (WMCNL-2514): personal access token until the GitHub App is installed on Wamocon.
    # When set, it replaces the App flow completely.
    token: str = field(default="", repr=False)

    @property
    def auth_mode(self) -> str:
        return "personal token (temporary)" if self.token else "GitHub App"


@dataclass
class LiteLLMSettings:
    base_url: str
    api_key: str


@dataclass
class MinioSettings:
    endpoint: str
    access_key: str
    secret_key: str


@dataclass
class VoiceSettings:
    stt_url: str
    tts_url: str


@dataclass
class TelegramSettings:
    bot_token: str
    engineer_chat_id: str
    ceo_chat_id: str


@dataclass
class JarvisConfig:
    version: int
    project: str
    sandbox: SandboxConfig
    models: ModelsConfig
    limits: LimitsConfig
    git: GitConfig
    jira: JiraSettings
    github: GitHubSettings
    litellm: LiteLLMSettings
    minio: MinioSettings
    voice: VoiceSettings
    telegram: TelegramSettings


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Required environment variable {name} is not set")
    return value


def _env_default(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _github_settings() -> GitHubSettings:
    """GITHUB_TOKEN set: personal token (temporary, WMCNL-2514), the App variables are not needed.
    Otherwise the GitHub App: GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH are required."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return GitHubSettings(
        app_id=os.environ.get("GITHUB_APP_ID", "") if token else _require_env("GITHUB_APP_ID"),
        private_key_path=os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "") if token else _require_env("GITHUB_APP_PRIVATE_KEY_PATH"),
        org=_require_env("GITHUB_ORG"),
        pilot_repo=_require_env("GITHUB_PILOT_REPO"),
        token=token,
    )


def load_config(path: str | Path = "jarvis.yaml") -> JarvisConfig:
    load_dotenv()

    yaml_path = Path(path)
    if not yaml_path.exists():
        raise ConfigError(f"Config file not found: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Fail fast and clearly if JIRA_API_TOKEN is missing (rule 10).
    jira_api_token = _require_env("JIRA_API_TOKEN")

    return JarvisConfig(
        version=raw["version"],
        project=raw["project"],
        sandbox=SandboxConfig(**raw["sandbox"], url=_env_default("SANDBOX_URL", "http://192.168.178.75:8002")),
        models=ModelsConfig(**raw["models"]),
        limits=LimitsConfig(**raw["limits"]),
        git=GitConfig(**raw["git"]),
        jira=JiraSettings(
            base_url=_require_env("JIRA_BASE_URL"),
            email=_require_env("JIRA_EMAIL"),
            api_token=jira_api_token,
        ),
        github=_github_settings(),
        litellm=LiteLLMSettings(
            base_url=_require_env("LITELLM_BASE_URL"),
            api_key=_require_env("JARVIS_API_KEY"),
        ),
        minio=MinioSettings(
            endpoint=_env_default("MINIO_ENDPOINT", "http://192.168.178.75:9010"),
            access_key=_require_env("MINIO_ACCESS_KEY"),  # credentials: no built-in default
            secret_key=_require_env("MINIO_SECRET_KEY"),
        ),
        # WHISPER_URL / SPEECH_URL are the current names; STT_URL / TTS_URL are still accepted.
        voice=VoiceSettings(
            stt_url=_env_default("WHISPER_URL", _env_default("STT_URL", "http://192.168.178.64:8787")),
            tts_url=_env_default("SPEECH_URL", _env_default("TTS_URL", "http://192.168.178.64:8788")),
        ),
        telegram=TelegramSettings(
            bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
            engineer_chat_id=_require_env("TELEGRAM_CHAT_ID"),
            ceo_chat_id=_require_env("TELEGRAM_CEO_CHAT_ID"),
        ),
    )
