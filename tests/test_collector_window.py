from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.sync import ChatSyncMetrics, iter_initial_messages, is_within_sync_window, sync_cutoff


def settings(**values) -> Settings:
    return Settings(
        _env_file=None,
        DB_DSN="postgresql+asyncpg://user:pass@localhost:5432/db",
        **values,
    )


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.kwargs = None

    async def iter_messages(self, _entity, **kwargs):
        self.kwargs = kwargs
        for message in self.messages:
            yield message


@pytest.mark.asyncio
async def test_initial_sync_includes_exact_boundary_and_stops_after_old_message():
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=168)
    messages = [
        SimpleNamespace(id=3, date=now),
        SimpleNamespace(id=2, date=cutoff),
        SimpleNamespace(id=1, date=cutoff - timedelta(microseconds=1)),
        SimpleNamespace(id=0, date=cutoff - timedelta(days=1)),
    ]
    client = FakeClient(messages)
    metrics = ChatSyncMetrics()

    result = [
        message.id
        async for message in iter_initial_messages(
            settings(),
            object(),
            client,
            None,
            now=now,
            metrics=metrics,
        )
    ]

    assert result == [3, 2]
    assert metrics.skipped_old == 1
    assert client.kwargs == {}


@pytest.mark.asyncio
async def test_incremental_sync_uses_last_message_id_and_keeps_window_guard():
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    client = FakeClient([SimpleNamespace(id=11, date=now)])

    result = [
        message.id
        async for message in iter_initial_messages(settings(), object(), client, 10, now=now)
    ]

    assert result == [11]
    assert client.kwargs == {"min_id": 10}


def test_naive_telegram_date_is_compared_as_utc():
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    cutoff = sync_cutoff(settings(), now)

    assert is_within_sync_window(SimpleNamespace(date=cutoff.replace(tzinfo=None)), cutoff)
    assert not is_within_sync_window(
        SimpleNamespace(date=(cutoff - timedelta(seconds=1)).replace(tzinfo=None)),
        cutoff,
    )
