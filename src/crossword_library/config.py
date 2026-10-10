"""Settings loaded from environment variables."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time


class ConfigError(Exception):
    """A required setting is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    puzzmo_token: str | None
    sync_time: time
    download_delay_seconds: float
    sync_since: date


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise ConfigError("DATABASE_URL is required")

    raw_time = env.get("SYNC_TIME", "06:00")
    try:
        sync_time = time.fromisoformat(raw_time)
    except ValueError:
        raise ConfigError(f"SYNC_TIME must be HH:MM (UTC), got {raw_time!r}") from None
    if sync_time.tzinfo is not None:
        raise ConfigError(f"SYNC_TIME must be HH:MM (UTC) without an offset, got {raw_time!r}")

    raw_delay = env.get("DOWNLOAD_DELAY_SECONDS", "3")
    try:
        delay = float(raw_delay)
    except ValueError:
        raise ConfigError(f"DOWNLOAD_DELAY_SECONDS must be a number, got {raw_delay!r}") from None
    if not math.isfinite(delay) or delay < 0:
        raise ConfigError(
            f"DOWNLOAD_DELAY_SECONDS must be a finite number >= 0, got {raw_delay!r}"
        )

    raw_since = env.get("SYNC_SINCE", "2023-10")
    try:
        year, month = raw_since.split("-")
        since = date(int(year), int(month), 1)
    except ValueError:
        raise ConfigError(f"SYNC_SINCE must be YYYY-MM, got {raw_since!r}") from None

    return Settings(
        database_url=database_url,
        puzzmo_token=env.get("PUZZMO_TOKEN") or None,
        sync_time=sync_time,
        download_delay_seconds=delay,
        sync_since=since,
    )
