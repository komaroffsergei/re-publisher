from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError

import app.sync as sync_module
from app.config import Settings
from app.sync import ChatSyncMetrics, iter_initial_messages, is_within_sync_window, sync_chat, sync_cutoff


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


@pytest.mark.asyncio
async def test_resume_scans_older_messages_without_repeating_saved_ones():
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    client = FakeClient([SimpleNamespace(id=2, date=now)])

    result = [
        message.id
        async for message in iter_initial_messages(
            settings(), object(), client, 1, before_message_id=3, now=now
        )
    ]

    assert result == [2]
    assert client.kwargs == {"min_id": 1, "offset_id": 3}


def test_naive_telegram_date_is_compared_as_utc():
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    cutoff = sync_cutoff(settings(), now)

    assert is_within_sync_window(SimpleNamespace(date=cutoff.replace(tzinfo=None)), cutoff)
    assert not is_within_sync_window(
        SimpleNamespace(date=(cutoff - timedelta(seconds=1)).replace(tzinfo=None)),
        cutoff,
    )


@pytest.mark.asyncio
async def test_flood_wait_resumes_after_last_saved_message(monkeypatch):
    now = datetime.now(timezone.utc)
    calls = []
    saved = []
    sync_states = []

    class FloodingClient:
        async def iter_messages(self, _entity, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                yield SimpleNamespace(id=3, date=now)
                raise FloodWaitError(None, 0)
            yield SimpleNamespace(id=2, date=now)
            yield SimpleNamespace(id=1, date=now - timedelta(days=8))

    @asynccontextmanager
    async def fake_session_scope(_factory):
        yield object()

    async def noop(*_args, **_kwargs):
        return None

    async def save(_client, _settings, _session, _chat, message, **_kwargs):
        saved.append(message.id)
        return message.id, True, "missing"

    async def update(_session, _peer_id, message_id=None, **_kwargs):
        sync_states.append(message_id)

    monkeypatch.setattr(sync_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(sync_module, "upsert_chat", noop)
    monkeypatch.setattr(sync_module, "get_last_message_id", noop)
    monkeypatch.setattr(sync_module, "save_message", save)
    monkeypatch.setattr(sync_module, "update_sync_state", update)
    monkeypatch.setattr(sync_module, "sleep_for_flood_wait", noop)

    metrics = await sync_chat(
        FloodingClient(),
        settings(),
        object(),
        SimpleNamespace(peer_id=42, entity=object(), title="chat"),
    )

    assert saved == [3, 2]
    assert calls == [{}, {"offset_id": 3}]
    assert sync_states == [3]
    assert metrics.inserted == 2
