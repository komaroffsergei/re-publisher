from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


from app.live_collector import eligible
from app.pipeline_coordinator import failure, reset_retry


def test_new_boundary_is_strict_about_unknown_dates_and_timezones():
    boundary = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert not eligible(SimpleNamespace(date=None), boundary)
    assert not eligible(SimpleNamespace(date=boundary - timedelta(seconds=1)), boundary)
    assert eligible(SimpleNamespace(date=boundary), boundary)
    assert eligible(
        SimpleNamespace(date=boundary.astimezone(timezone(timedelta(hours=3)))),
        boundary,
    )


def test_three_retries_then_stop_and_explicit_reset():
    entry = SimpleNamespace(auto_attempts=0, auto_phase=None, auto_state="pending")
    for attempt, seconds in enumerate((10, 30, 120), 1):
        failure(entry, "media", "network", True)
        assert entry.auto_state == "pending" and entry.auto_attempts == attempt
        assert (
            seconds - 1
            <= (entry.auto_retry_at - datetime.now(timezone.utc)).total_seconds()
            <= seconds
        )
    failure(entry, "media", "network", True)
    assert entry.auto_state == "stopped"
    reset_retry(entry)
    assert (
        entry.auto_attempts == 0
        and entry.auto_retry_at is None
        and entry.last_error is None
    )


def test_known_missing_media_does_not_retry_forever():
    entry = SimpleNamespace(auto_attempts=0, auto_phase=None, auto_state="pending")
    failure(entry, "media", "skipped_too_large")
    assert entry.auto_state == "blocked" and entry.auto_attempts == 0
