"""Application configuration for the Employee Onboarding service."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All application settings, sourced from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://onboard_user:onboard_pass@localhost:5432/onboarding_db"

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/1"

    # ── SMTP ─────────────────────────────────────────────────────────────────
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""

    # ── Slack ─────────────────────────────────────────────────────────────────
    slack_bot_token: str = ""
    slack_default_channel: str = "#general"

    # ── Jira ─────────────────────────────────────────────────────────────────
    jira_url: str = ""
    jira_user: str = ""
    jira_api_token: str = ""

    # ── HR Portal ────────────────────────────────────────────────────────────
    hr_portal_url: str = "https://hr.example.com"
    hr_portal_user: str = ""
    hr_portal_pass: str = ""

    # ── File Paths ────────────────────────────────────────────────────────────
    docs_output_dir: str = "/data/documents"
    screenshots_dir: str = "/data/screenshots"

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Company ───────────────────────────────────────────────────────────────
    company_name: str = "Acme Corporation"
    welcome_email_from: str = ""

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def docs_output_path(self) -> Path:
        """Return docs output directory as a resolved Path."""
        p = Path(self.docs_output_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def screenshots_path(self) -> Path:
        """Return screenshots directory as a resolved Path."""
        p = Path(self.screenshots_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def celery_broker_url(self) -> str:
        return self.redis_url

    @property
    def celery_result_backend(self) -> str:
        return self.redis_url

    @property
    def from_email(self) -> str:
        """Sender address: prefers welcome_email_from, falls back to smtp_user."""
        return self.welcome_email_from or self.smtp_user

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return upper


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
