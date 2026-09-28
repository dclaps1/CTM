"""Application settings, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    ctm_access_key: str = ""
    ctm_secret_key: str = ""
    ctm_account_id: str = ""
    ctm_base_url: str = "https://api.calltrackingmetrics.com"
    database_url: str = "sqlite:///./ctm.db"
    secret_key: str = "change-me"
    session_https_only: bool = False
    timezone: str = "America/New_York"
    sync_interval_minutes: int = 10
    initial_sync_days: int = 30
    webhook_token: str = ""
    push_scores_to_ctm: bool = False

    @property
    def ctm_configured(self) -> bool:
        return bool(self.ctm_access_key and self.ctm_secret_key and self.ctm_account_id)


@lru_cache
def get_settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    defaults = Settings()
    return Settings(
        ctm_access_key=env("CTM_ACCESS_KEY", ""),
        ctm_secret_key=env("CTM_SECRET_KEY", ""),
        ctm_account_id=env("CTM_ACCOUNT_ID", ""),
        ctm_base_url=env("CTM_BASE_URL") or defaults.ctm_base_url,
        database_url=env("DATABASE_URL") or defaults.database_url,
        secret_key=env("SECRET_KEY") or defaults.secret_key,
        session_https_only=_bool(env("SESSION_HTTPS_ONLY")),
        timezone=env("TIMEZONE") or defaults.timezone,
        sync_interval_minutes=_int(env("SYNC_INTERVAL_MINUTES"), defaults.sync_interval_minutes),
        initial_sync_days=_int(env("INITIAL_SYNC_DAYS"), defaults.initial_sync_days),
        webhook_token=env("WEBHOOK_TOKEN", ""),
        push_scores_to_ctm=_bool(env("PUSH_SCORES_TO_CTM")),
    )
