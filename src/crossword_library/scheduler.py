"""Run a sync once a day at a fixed UTC time."""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone

from .sync import SyncAlreadyRunning

log = logging.getLogger(__name__)


def next_run_at(now: datetime, at: time) -> datetime:
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def serve(
    sync_time: time,
    run_once: Callable[[], object],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = _time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    while not should_stop():
        target = next_run_at(now(), sync_time)
        log.info("next sync at %s", target.isoformat())
        sleep(max(0.0, (target - now()).total_seconds()))
        try:
            run_once()
        except SyncAlreadyRunning:
            log.warning("skipping scheduled sync: another sync is running")
        except Exception:
            log.exception("scheduled sync failed")
