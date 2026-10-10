from datetime import datetime, time, timedelta, timezone

from crossword_library.scheduler import next_run_at, serve
from crossword_library.sync import SyncAlreadyRunning

UTC = timezone.utc


def test_next_run_later_today():
    assert next_run_at(datetime(2026, 10, 10, 5, 0, tzinfo=UTC), time(6, 0)) == datetime(
        2026, 10, 10, 6, 0, tzinfo=UTC)


def test_next_run_tomorrow_when_passed_or_exact():
    assert next_run_at(datetime(2026, 10, 10, 6, 0, tzinfo=UTC), time(6, 0)) == datetime(
        2026, 10, 11, 6, 0, tzinfo=UTC)
    assert next_run_at(datetime(2026, 12, 31, 7, 0, tzinfo=UTC), time(6, 0)) == datetime(
        2027, 1, 1, 6, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start):
        self.current = start

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def test_serve_runs_daily_and_survives_errors():
    clock = Clock(datetime(2026, 10, 10, 5, 0, tzinfo=UTC))
    runs = []
    outcomes = iter([None, RuntimeError("boom"), SyncAlreadyRunning("busy")])

    def run_once():
        runs.append(clock.now())
        outcome = next(outcomes)
        if outcome:
            raise outcome

    serve(time(6, 0), run_once, now=clock.now, sleep=clock.sleep,
          should_stop=lambda: len(runs) >= 3)
    assert runs == [
        datetime(2026, 10, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 10, 11, 6, 0, tzinfo=UTC),
        datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
    ]
