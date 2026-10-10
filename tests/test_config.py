from datetime import date, time

import pytest

from crossword_library.config import ConfigError, load_settings

BASE = {"DATABASE_URL": "postgresql://u:p@localhost/db"}


def test_defaults():
    settings = load_settings(BASE)
    assert settings.database_url == "postgresql://u:p@localhost/db"
    assert settings.puzzmo_token is None
    assert settings.sync_time == time(6, 0)
    assert settings.download_delay_seconds == 3.0
    assert settings.sync_since == date(2023, 10, 1)


def test_overrides():
    settings = load_settings(
        BASE
        | {
            "PUZZMO_TOKEN": "tok",
            "SYNC_TIME": "07:30",
            "DOWNLOAD_DELAY_SECONDS": "0.5",
            "SYNC_SINCE": "2025-01",
        }
    )
    assert settings.puzzmo_token == "tok"
    assert settings.sync_time == time(7, 30)
    assert settings.download_delay_seconds == 0.5
    assert settings.sync_since == date(2025, 1, 1)


def test_blank_token_is_none():
    assert load_settings(BASE | {"PUZZMO_TOKEN": ""}).puzzmo_token is None


def test_database_url_required():
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings({})


@pytest.mark.parametrize(
    "key,value",
    [
        ("SYNC_TIME", "6am"),
        ("DOWNLOAD_DELAY_SECONDS", "fast"),
        ("SYNC_SINCE", "2023-10-05"),
        ("SYNC_SINCE", "October"),
    ],
)
def test_invalid_values(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings(BASE | {key: value})


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf"])
def test_download_delay_must_be_finite_and_non_negative(value):
    with pytest.raises(ConfigError, match="DOWNLOAD_DELAY_SECONDS"):
        load_settings(BASE | {"DOWNLOAD_DELAY_SECONDS": value})


def test_sync_time_with_utc_offset_rejected():
    with pytest.raises(ConfigError, match="SYNC_TIME"):
        load_settings(BASE | {"SYNC_TIME": "06:00+13:00"})
